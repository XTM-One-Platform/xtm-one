# SPDX-License-Identifier: Apache-2.0

"""A2A tables — inbound exposure register, outbound settings and delegations.

Seven tables. Inbound: ``A2AExposure`` is the per-agent opt-in register,
``A2AContext`` the protocol's ``contextId`` owned by one partner, ``A2ATask``
the protocol-shaped view of an external submission and ``A2APushConfig`` a
partner's callback for one task. ``A2AIntegrationSettings`` carries the
**direction** every A2A integration has. Outbound: ``A2AClientTarget`` is one
partner agent this platform can task, with its verification and circuit
state, and ``A2ADelegation`` is the record of work sent to a partner.

No table stores anything the platform already knows. In particular
``A2ATask`` deliberately does NOT mirror run state: it references the
``AssignmentRun`` that holds it, because two copies of "what state is this
task in" would eventually disagree, and the partner-visible copy is exactly
the one that must not drift.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    asc,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Why an exposure stopped resolving. Recorded rather than inferred so an
# operator can tell a deliberate revocation from one the platform forced.
#
# There is deliberately NO ``owner_deactivated`` reason. FR-007's "the user it
# executes as [is] deactivated" is enforced as a live filter in
# ``a2a.exposure._resolvable_exposures``, not as a revocation, because
# deactivation is reversible and revocation is not: suspending an account for a
# week must not permanently destroy a partner integration.
# HISTORICAL. Written by the explicit revoke endpoint, which no longer exists
# — exposure follows an integration binding, so an operator turning it off
# produces ``unbound`` instead. Retained because rows created before that
# change still carry the value and the operator guide still explains it.
REVOKED_MANUAL = "manual"
REVOKED_TIER_CHANGE = "tier_change"
REVOKED_SHARING_CHANGE = "sharing_change"
REVOKED_AGENT_DISABLED = "agent_disabled"
# An owner transfer moves a personally-owned agent's run-as identity out from
# under a live exposure, so it must revoke — see ``a2a.hooks._WATCHED``.
REVOKED_OWNER_CHANGE = "owner_change"
# Exposure is implicit in binding an A2A integration, so removing the last
# such binding is the operator saying "stop being reachable" — the same act
# the old explicit toggle performed.
REVOKED_UNBOUND = "unbound"

# The mirror of REVOKED_UNBOUND, and the only way an agent becomes reachable.
# Not a stored column value — there is no "exposure reason" on the row — but
# the telemetry label for the expose direction (``a2a.metrics``), spelled here
# so the pair stays together and a reader finds both at once.
EXPOSED_BOUND = "bound"


class A2AExposure(Base):
    """A single opt-in making one agent reachable by external A2A clients.

    A revoked row is retained rather than deleted. That retention is what
    makes "a revoked card URL never resolves again" enforceable: the retired
    token stays on record, so a replayed URL can be recognised as dead rather
    than merely unknown.
    """

    __tablename__ = "a2a_exposures"
    __table_args__ = (
        # At most one LIVE exposure per agent, enforced by the database rather
        # than by an application check: two concurrent grants landing on
        # different replicas must not both succeed.
        Index(
            "uq_a2a_exposures_live_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Opaque, and minted fresh on every exposure - never derived from the
    # agent's UUID, and never reused across rows even after revocation.
    card_token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    # The identity external work executes as, mirroring
    # ``Assignment.run_as_user_id`` - the platform's existing answer for
    # "whose data does a company-managed agent see". Frozen at exposure time.
    #
    # There is deliberately no company-scoped alternative to reach for: a
    # company-managed agent has no data access of its own anywhere in this
    # platform. Interactive chat borrows the chatting user's identity, the MCP
    # endpoint the caller's, an assignment runs as
    # ``run_as_user_id or assignment.user_id``. External work is the one path
    # with no invoking human, which is why the choice is recorded here.
    #
    # NOT NULL: resolution happens once, when the exposure is created, and an
    # external task has no invoking human to fall back to later. Exposure is
    # implicit in binding an A2A integration, so there is no moment at which
    # to demand a choice — ``resolve_run_as`` inherits the agent's owner,
    # including for a company-managed agent, where that is whoever seeded it.
    # That trade-off is documented at ``exposure.resolve_run_as``; it is why
    # this identity is surfaced in the UI, notified, and audit-logged.
    #
    # Legacy rows may hold an administrator's explicit nomination instead,
    # from the removed expose endpoint. Nothing distinguishes the two here,
    # and nothing needs to: whichever it was, it is pinned and authoritative.
    #
    # Never the actor who bound the integration: a config-tier group member
    # may edit someone else's agent, and doing so must not lend their reach.
    run_as_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Recorded for audit ONLY. Never consulted when resolving access, and
    # NULL on everything created since exposure became implicit: the flush
    # hook that creates an exposure runs in ``before_flush`` and has no
    # request context to read an actor from. Who made the binding edit is in
    # the agent's own audit trail; this column preserves the answer for rows
    # the old explicit expose endpoint wrote.
    exposed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # There is deliberately NO Entra-federation state on this table. The
    # optional federated credential form (see ``a2a.federation``) is an
    # ``entra_a2a`` integration bound through the agent's ordinary
    # ``integration_bindings`` — the same Tools-tab mechanism as every other
    # integration — and resolved per request from the agent, so it needs no
    # exposure-side mirror that could drift.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # The per-exposure opt-in of #2202, off by default: a partner's task on
    # this exposure also reads the work items of the exposed agent's HOME
    # fleet (``a2a.partner_runs.partner_fleet_work_items_clause``), never the
    # run-as identity's wider reach. Read at query time, so switching it off
    # narrows the next call of a task already running.
    partner_fleet_reads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None


class A2AContext(Base):
    """The protocol's conversational grouping, owned by one partner.

    The protocol's ``contextId`` as a row rather than a per-task value: who may
    continue it, who answers it now, and the card it was created under. A
    transfer owns the conversation the specification says it owns because that
    ownership has somewhere to live.

    Minted here and never accepted from a partner. A submission naming an
    identifier this deployment did not issue is answered exactly as one naming
    another partner's, so the existence of a context cannot be probed.
    """

    __tablename__ = "a2a_contexts"

    __table_args__ = (
        # Every read is ownership-scoped, the same shape as the composite on
        # a2a_tasks: a context is only ever looked up as "this client's".
        Index("ix_a2a_contexts_client_id_ctx", "client_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The registration that created it. Not a foreign key for the same reason
    # A2ATask.client_id is not: a registration is identified by its OAuth
    # client_id string, and the row it belongs to differs by credential form.
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Who answers the next message. Starts as the addressed agent and follows a
    # transfer; consultation and background delegation do not move it, because
    # neither claims the messages that come after.
    responsible_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The card the partner addressed, frozen at creation. A transfer changes who
    # answers, never which contract the partner validated its request against.
    exposure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class A2APushConfig(Base):
    """A partner's callback for one task.

    The platform holds no key material for this: the partner decides how its own
    endpoint is protected and registers the credential to present — an opaque
    ``token`` echoed back, or a scheme and credentials sent as that scheme's
    header. Both are secrets and are encrypted at rest like every other.

    A notification carries the task's identity and state and no content, so a
    forgery discloses nothing. What the credential prevents is a partner being
    driven to call ``tasks/get`` on a false signal.
    """

    __tablename__ = "a2a_push_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The owning registration. A config is invisible to any other client, the
    # same scoping every read on this surface applies.
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    url: Mapped[str] = mapped_column(Text, nullable=False)

    # Fernet-encrypted. Echoed to the partner on delivery, never logged.
    token: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The scheme the partner's endpoint expects, when it registered one instead
    # of a bare token, and the credentials to present under it.
    auth_scheme: Mapped[str | None] = mapped_column(String(40), nullable=True)
    auth_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class A2ATask(Base):
    """An external submission, mirroring the AssignmentRun that executes it."""

    __tablename__ = "a2a_tasks"
    __table_args__ = (
        # The durable half of deduplication. Redis provides the fast path and
        # can be lost; this constraint is what actually guarantees a partner
        # retry cannot start a second billable run.
        #
        # PARTIAL, excluding rejected rows. A submission refused for bad
        # content must not consume its message id: the partner was told what
        # was wrong and is expected to fix it and re-send under the same id.
        # A full unique index would make that corrected retry collide with
        # the rejection it is correcting.
        Index(
            "uq_a2a_tasks_client_message",
            "client_id",
            "message_id",
            unique=True,
            postgresql_where=text("rejected_reason IS NULL"),
        ),
        Index("ix_a2a_tasks_client_id_task", "client_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The context this task belongs to. No default: a random identifier would
    # satisfy the column and point at no context row, which is exactly the
    # grouping-nothing state this replaced.
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exposure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_exposures.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The OAuth client_id of the registration that created this task. Reads
    # are scoped to it, so one partner can never see another's work.
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # NULL only between row creation and run creation, and permanently for a
    # task rejected before any run started.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Which agent produced the answer, when a transfer moved it off the agent
    # the partner addressed. NULL means no transfer, never "unknown". No FK:
    # deleting the agent later must neither erase the attribution nor block.
    responding_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # What the partner spoke when it submitted. Recorded rather than
    # re-derived: a notification is spelled in the partner's own vocabulary and
    # is sent long after the request that negotiated it is over. NULL means a
    # task from before this was recorded, which is v0.3.
    protocol_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    inbound_parts: Mapped[list | None] = mapped_column(JSON, nullable=True)

    rejected_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Cancellation must cross replicas: the request cancelling a task rarely
    # lands on the replica running it, so the in-process asyncio event alone
    # would silently do nothing.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Reaper bookkeeping. A run whose replica died leaves a stale heartbeat;
    # without this the task stays pollable forever.
    lease_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    reaped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Set once the task's terminal state has been published and its callbacks
    # told. Recorded rather than derived: "have we said this yet" is not a fact
    # about the task's state, and without it the sweep that catches a missed
    # announcement would repeat it every minute.
    terminal_announced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: What this task asked its caller, while the run that asked is over.
    #: Set makes the task report ``input-required``; cleared by the answer.
    #: A2A blocks a task on its CLIENT, so this is a question for the calling
    #: AGENT and never for a person — nothing here parks for human input.
    caller_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Rounds of clarification this task has had. Bounded: two agents can ask
    #: each other for precision indefinitely, and neither gets bored.
    clarification_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


# --- Outbound (A2A client) ------------------------------------------------

#: Direction values. Exactly one per integration, never both: the two
#: directions need different credential fields and confer opposite
#: capabilities, so an integration serving both would be one row with two
#: credential sets and two blast radii (FR-055).
DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"

#: Why an outbound registration is unusable. A CLOSED set: the classifier in
#: ``a2a.client.failures`` is the only producer, so no call site can invent a
#: reason that no surface knows how to explain.
UNUSABLE_NEVER_VERIFIED = "never_verified"
UNUSABLE_CARD_UNFETCHABLE = "card_unfetchable"
UNUSABLE_CARD_MALFORMED = "card_malformed"
UNUSABLE_TRANSPORT_UNVERIFIED = "transport_unverified"
UNUSABLE_SIGNATURE_INVALID = "signature_invalid"
UNUSABLE_NO_COMMON_VERSION = "no_common_protocol_version"
#: The partner serves no transport binding this platform speaks - gRPC only,
#: say. Distinct from having no common VERSION: the two are different
#: negotiations, they fail for different reasons, and an operator told "no
#: common version" about a gRPC-only partner would go looking for a version
#: setting that would never have helped.
UNUSABLE_NO_COMMON_TRANSPORT = "no_common_transport"
#: The partner refused the REQUEST we sent - a 4xx on the task surface that is
#: not about credentials. A fault on this side, and its own reason distinct
#: from ``card_malformed``: the card is fine, the message body was refused,
#: and an operator must not be sent to re-check a card URL. A wrong word
#: costs more than no word.
UNUSABLE_REQUEST_REJECTED = "request_rejected_by_partner"
# A machine-readable REASON code surfaced in the health verdict, not a
# credential: it names what failed and never carries a value. Flagged by
# bandit's hardcoded-password heuristic on the word "token" alone.
UNUSABLE_CREDENTIAL_NO_TOKEN = "credential_cannot_mint_token"  # noqa: S105
UNUSABLE_CREDENTIAL_REJECTED = "credential_rejected_by_partner"
UNUSABLE_DESTINATION_NOT_PERMITTED = "destination_not_permitted"
UNUSABLE_DESTINATION_BLOCKED = "destination_blocked_by_deployment"
UNUSABLE_ORIGIN_MISMATCH = "origin_mismatch"
UNUSABLE_CARD_CHANGE_UNACCEPTED = "card_change_unaccepted"
UNUSABLE_SIGNATURE_DOWNGRADED = "signature_downgraded"
UNUSABLE_VERSION_DOWNGRADED = "protocol_version_downgraded"

UNUSABLE_REASONS: frozenset[str] = frozenset(
    {
        UNUSABLE_NEVER_VERIFIED,
        UNUSABLE_CARD_UNFETCHABLE,
        UNUSABLE_CARD_MALFORMED,
        UNUSABLE_TRANSPORT_UNVERIFIED,
        UNUSABLE_SIGNATURE_INVALID,
        UNUSABLE_NO_COMMON_VERSION,
        UNUSABLE_NO_COMMON_TRANSPORT,
        UNUSABLE_REQUEST_REJECTED,
        UNUSABLE_CREDENTIAL_NO_TOKEN,
        UNUSABLE_CREDENTIAL_REJECTED,
        UNUSABLE_DESTINATION_NOT_PERMITTED,
        UNUSABLE_DESTINATION_BLOCKED,
        UNUSABLE_ORIGIN_MISMATCH,
        UNUSABLE_CARD_CHANGE_UNACCEPTED,
        UNUSABLE_SIGNATURE_DOWNGRADED,
        UNUSABLE_VERSION_DOWNGRADED,
    }
)

#: Health verdict values (FR-020).
USABILITY_USABLE = "usable"
USABILITY_LIMITED = "limited"
USABILITY_UNUSABLE = "unusable"

#: Whether the card a registration operates against proved itself. NULL until
#: a first verdict exists — a row is created before any card is fetched, and
#: neither value is true of a registration never verified (FR-017).
SIGNATURE_VERIFIED = "verified"
SIGNATURE_UNSIGNED = "unsigned"

#: One classification, two consumers: the circuit breaker and the health
#: verdict both read it, so they cannot disagree about the same partner
#: (FR-053).
FAILURE_TRANSIENT = "transient"
FAILURE_DURABLE = "durable"


class A2AIntegrationSettings(Base):
    """A2A-specific state for one integration: its direction, and if outbound,
    its verification and circuit state.

    **Why a side table rather than columns on ``integrations``**: direction
    must be QUERYABLE — the exposure hook resolves it for a set of integration
    ids on every relevant flush — which rules out ``extra_config_enc``, and it
    is A2A-specific, which rules out adding it to the table all ~130 providers
    share.

    It carries DIRECTION and nothing else. Everything an outbound registration
    verifies about a partner is per-CARD, not per-integration, and lives in
    :class:`A2AClientTarget` — one partner exposes as many agents as it likes,
    each with its own card, its own verdict and its own circuit.

    **The rule that protects the shipped inbound surface**: an A2A integration
    with NO row here belongs to neither direction — it exposes nothing and
    contributes no tool. A missing row therefore degrades to a visible,
    recoverable "my agent is not exposed" and can never become an unintended
    exposure. The migration backfill and the create path both guarantee a row,
    so this is a backstop, not a normal state.
    """

    __tablename__ = "a2a_integration_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Unique: one settings row per integration. CASCADE is FR-008 — deleting
    # the integration removes its credentials and this row in one act, so no
    # orphan verdict can outlive the registration it describes.
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # The one value the exposure hook reads. NOT NULL: there is no valid A2A
    # integration without a direction, and a nullable column here would
    # reintroduce the ambiguity the missing-row rule exists to resolve.
    direction: Mapped[str] = mapped_column(String(8), nullable=False)

    # Outbound only: the state of the partner credential, written by
    # ``a2a.client.credential_state`` (#12346). The integration's own status
    # follows it (``error`` while ``credential_error_code`` is set), so a
    # rejected credential is visible on the integration, not only on its
    # per-card targets. Never a secret: a code, the mint's error text, times.
    last_token_minted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credential_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    credential_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Inbound ``oauth_a2a`` only: the last token request this integration's
    # credential refused (#105), shown to its managers so an operator can tell
    # a client id the partner still holds from a mistyped secret. Never a
    # secret: a time, the client id the caller presented in clear, a reason
    # (``unknown_client``, ``secret_mismatch``, ``no_reachable_agent``;
    # ``app.a2a.refusals``).
    last_refused_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refused_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_refused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The client ids an explicit "Rotate client id and secret" retired, newest
    # last and bounded, so a partner still presenting one is reported as a
    # retired client id rather than one nobody issued. Identifiers, not secrets.
    retired_client_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_outbound(self) -> bool:
        return self.direction == DIRECTION_OUTBOUND

    @property
    def is_inbound(self) -> bool:
        return self.direction == DIRECTION_INBOUND


#: Terminal classification of one delegation. The five FR-029 classes a
#: calling agent must be able to tell apart, plus the two that never leave
#: this platform.
OUTCOME_COMPLETED = "completed"
OUTCOME_PARTNER_FAILED = "partner_failed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_CREDENTIAL_REJECTED = "credential_rejected"
OUTCOME_CONTENT_TYPE_MISMATCH = "content_type_mismatch"
OUTCOME_REFUSED_PREFLIGHT = "refused_preflight"
OUTCOME_CANCELLED = "cancelled"

DELEGATION_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_COMPLETED,
        OUTCOME_PARTNER_FAILED,
        OUTCOME_TIMEOUT,
        OUTCOME_UNREACHABLE,
        OUTCOME_CREDENTIAL_REJECTED,
        OUTCOME_CONTENT_TYPE_MISMATCH,
        OUTCOME_REFUSED_PREFLIGHT,
        OUTCOME_CANCELLED,
    }
)

#: What a delegation reports when the attempt ended with the partner's task
#: still running. A RESULT discriminator, never a stored outcome: the row keeps
#: ``outcome`` NULL, because this is the absence of a conclusion rather than one
#: of them. Kept out of ``DELEGATION_OUTCOMES`` for the same reason.
OUTCOME_STILL_RUNNING = "still_running"

#: An outstanding delegation — one whose partner task is still running and
#: whose calling agent has been handed it back — is a row with no outcome and
#: no ``terminal_at``. There is deliberately no state column and no
#: "outstanding" member of ``DELEGATION_OUTCOMES``: that set is the terminal
#: classifications, and the absence of one is what "still running" means.
#:
#: How a delegation waited (FR-020). Recorded on the row rather than derived
#: from the target, because a partner that declares callbacks may still not
#: send one — what happened is not what was hoped for.
MECHANISM_ASKED = "asked"
MECHANISM_CALLED_BACK = "called_back"

DELEGATION_MECHANISMS: frozenset[str] = frozenset(
    {MECHANISM_ASKED, MECHANISM_CALLED_BACK}
)


class A2AClientTarget(Base):
    """One agent this platform can task at a partner, and what we know of it.

    **Why this is a table and not columns on the registration.** A partner is
    a trust relationship: one credential, one token endpoint, one operator
    decision to trust it. What that partner EXPOSES is a different cardinality
    — an A2A agent card describes exactly one agent, so a partner offering
    three agents publishes three cards. Holding the card on the registration
    forced one registration per agent, which meant duplicating the same
    credential once per agent and revoking it in as many places.

    So the registration owns the partner and its credential; a row here owns
    one card. Everything below is per-card BECAUSE it is a fact about one
    agent rather than about the partner: a verdict, a pinned origin, a
    negotiated protocol version, a fingerprint, and a circuit. One agent being
    unreachable must not pause delegation to its siblings, which is precisely
    what a partner-wide circuit would do.

    The credential is deliberately NOT here. It belongs to the partner, and a
    per-target copy would be the same secret stored N times with N chances to
    drift — the duplication this table exists to remove.
    """

    __tablename__ = "a2a_client_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # CASCADE for the same reason the settings row cascades (FR-008): deleting
    # the integration retires the credential and everything verified under it
    # in one act, so no verdict outlives the registration it describes.
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Where the partner PUBLISHES ITS CARD. A discovery document, fetched with
    # GET — never the address work is sent to.
    #
    # NOT NULL: this row IS a card. A target without one describes no agent,
    # and a nullable column would also defeat the uniqueness below, since
    # PostgreSQL treats NULLs as distinct and would admit any number of them.
    card_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Where work is SENT: the endpoint the fetched card declares in its ``url``.
    # Distinct from ``card_url`` and almost never equal to it — the card URL
    # is a GET-only discovery document, and delegating to it answers "405
    # Method Not Allowed" against this platform's own inbound surface (and
    # worse than an error against a partner whose card route accepts POST).
    invocation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scheme + host + port the registration was verified against. Every
    # request is checked against it and a card declaring another origin is
    # refused, so a partner cannot move us mid-life (FR-034).
    pinned_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Recorded, never defaulted at request time (FR-023).
    agreed_protocol_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    # Which A2A binding ``invocation_url`` speaks - JSONRPC or HTTP+JSON.
    # Stored beside the endpoint because it is the same decision: a card
    # declares a SET of interfaces, and the one verification chose is an
    # (endpoint, binding, version) triple. Re-deriving the binding at request
    # time from a re-read card is how a partner would be able to move us onto
    # a wire we never verified.
    #
    # Nullable because a row exists before verification chooses a binding;
    # delegation reads a missing value as JSONRPC, the specification's own
    # default.
    transport: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Hash over the card's MATERIAL fields only — invocation endpoint, auth
    # scheme, input and output content types, signature presence. What
    # "materially changed" is computed against (FR-019).
    card_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_card_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    signature_state: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Fail-closed default. A row exists before verification, so a registration
    # that has not been verified — or whose first verification failed before
    # writing a verdict — is unusable rather than usable-by-omission
    # (constitution Principle II).
    usability: Mapped[str] = mapped_column(
        String(16), nullable=False, default=USABILITY_UNUSABLE, server_default=text("'unusable'")
    )
    unusable_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Operator-facing detail. Never a credential value: it is rendered in the
    # UI and written to the audit trail (FR-004).
    last_verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Transient failures ONLY. A durable failure never increments this — it
    # marks the registration unusable directly, because retrying it cannot
    # succeed (FR-053).
    consecutive_transient_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    circuit_open_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Where this partner said it lists the agents our credential reaches, from
    # the extension its card declares (``app.a2a.extensions``). ``None`` means
    # the card carried no such declaration.
    #
    # Stored per TARGET although the capability belongs to the partner,
    # because this is literally what one card said and nothing else can be
    # claimed honestly from parsing it. The reader aggregates — any target of
    # a registration declaring it makes the partner a discovery partner — and
    # gets self-healing for free: a partner that adds the extension later
    # starts being asked as soon as any one of its cards is re-verified.
    partner_discovery_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What the verified card declares, cached for display before the
    # registration is put to use (FR-018). Never edited here — the partner
    # authors its card and this platform only reads it.
    declared_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # What FR-028's pre-flight content-type check reads.
    declared_input_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    declared_output_types: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Whether this partner declares it will call back, refreshed by the same
    # sweep as the rest of the card. NULL means not verified since this was
    # recorded, which reads as "not declared" — the safe interpretation, and
    # the one that keeps a callback from being registered with a partner that
    # never offered one. Deliberately NOT fingerprinted: see
    # ``fingerprint_card``.
    declares_callbacks: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # One row per card per partner. Two rows for one card would verify the
        # same agent twice and give a calling model two tools that do one
        # thing, disagreeing the moment one verdict moved.
        UniqueConstraint("integration_id", "card_url", name="uq_a2a_target_card"),
        # What the sweep scans: the stalest verdict first, never-verified
        # before that.
        #
        # ``NULLS FIRST`` is not decoration. ``due_targets`` orders by
        # ``last_verified_at ASC NULLS FIRST``, and PostgreSQL's default btree
        # ordering is NULLS LAST — which cannot serve that ORDER BY in either
        # scan direction, so a plain index here would be built, maintained on
        # every write, and never used.
        #
        # No partial predicate: a target row is outbound by construction,
        # since an inbound registration has no card and owns no targets.
        Index("ix_a2a_client_targets_due", asc("last_verified_at").nullsfirst()),
    )


class A2ADelegation(Base):
    """One unit of work this platform handed to a partner.

    The outbound mirror of ``A2ATask``, and deliberately NOT symmetric with it
    in one respect. ``A2ATask.run_id`` is ``ON DELETE SET NULL`` because an
    inbound task is the PARTNER's record and must outlive its run: the partner
    holds the id and must still get a defined answer. A delegation is OUR
    record of work we sent, has no external holder, and FR-044 ties its
    lifetime to the run it belongs to — so ``run_ref`` cascades. ``SET NULL``
    would also break FR-046, since a continuation is authorised against this
    row — its integration always, and its ``run_ref`` when the caller has one
    (the chat path carries no run id) — and a nulled reference would turn
    every in-flight continuation into an ownership failure.

    Deliberately absent: the message sent and the result returned. Payloads
    live where run content already lives; copying them here would create a
    second store of partner-exchanged content with its own retention question.
    """

    __tablename__ = "a2a_delegations"
    __table_args__ = (
        # A registration's own activity list, newest first.
        Index("ix_a2a_delegations_integration_time", "integration_id", "submitted_at"),
        # FR-046's ownership check: is this partner task one THIS run created?
        Index("ix_a2a_delegations_run", "run_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The partner. Attribution keys on what was recorded HERE at submission
    # time and is never inferred from the agent's current bindings — two
    # registrations may be bound to one agent and would otherwise each report
    # the other's traffic (FR-042).
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # WHICH of the partner's agents this went to. Recorded for the same reason
    # the partner is: a registration reaching three agents would otherwise
    # report one undifferentiated stream, and "this target is failing" is
    # exactly the question an operator asks when one of three stops working.
    #
    # Nullable and SET NULL rather than CASCADE: removing a target must not
    # delete the history of work already sent through it. A row whose target
    # is gone still says what was sent, when, and how it ended.
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("a2a_client_targets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    # Whose boundary applied and whose usage was consumed (FR-037).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # ``assignment_runs`` is the platform's universal run record — the chat
    # path creates one too — so this single reference covers both run paths
    # FR-021 admits. NULL only between row creation and run resolution.
    run_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    partner_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The version THIS exchange used. Recorded rather than looked up later,
    # because the registration's may since have renegotiated upward.
    protocol_version: Mapped[str] = mapped_column(String(16), nullable=False)
    # Copied from the registration at submission. This is what makes FR-017's
    # verified/unverified distinction legible on the run that took the risk,
    # rather than only on the registration.
    signature_state: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # How this delegation waited. NULL on a row written before the mechanism
    # was recorded, which is every row that predates this column.
    mechanism: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When this side gave up on a silent partner. Distinct from ``terminal_at``
    # because the two answer different questions: ``terminal_at`` says the row
    # ended, this says it ended without anything being DECIDED. Every other
    # conclusion follows something that happened — the partner failed, the
    # credential was rejected, the ceiling ran out and the task was cancelled
    # at the partner. Abandonment cancels nothing, so the work may still be
    # running, and a callback that finally arrives is the answer the caller was
    # promised rather than a contradiction. This column is what lets the
    # callback route tell the two timeouts apart.
    abandoned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When an abandoned row was made outstanding again by a partner that
    # finally spoke. The floor measures SILENCE, and ``submitted_at`` is older
    # than the abandonment cutoff by definition for any row that reached this
    # state — so without a second stamp the next cycle re-abandons what a
    # callback re-opened seconds ago, and every retry of the partner's
    # delivery starts the loop again. ``submitted_at`` stays immutable: it is
    # what the history renders as the submission time.
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When this side decided to cancel the partner's task. Telling the partner
    # is detached (N partners are N network calls, off the path that asked),
    # so this is written before any partner is called - for a run tree in the
    # decision's own transaction - and a row still OUTSTANDING with it set is
    # a cancel a hard crash interrupted, which the client sweep re-sends. Kept
    # once the row concludes, like ``abandoned_at``: it is what a collect or a
    # callback that raced the cancel reads to leave the cancelled caller alone.
    # Distinct from abandonment, which cancels nothing.
    cancel_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Minted per delegation when a callback is registered, and presented back
    # by the partner. Encrypted at rest like every other partner credential,
    # and never reused across delegations — a leaked one exposes one exchange.
    callback_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The conversation a CHAT delegation was made in, where ``run_ref`` is
    # NULL. Without it an outstanding partner task has nothing to cancel it
    # when the user abandons the conversation: a chat turn ending must not
    # cancel — handing back is what ends it — so the conversation is the only
    # boundary left.
    # Taken by whichever replica acts on a partner's callback. Separate from
    # the outcome because the outcome is not known until the partner's task has
    # been read, and the read must happen once: two replicas resuming the same
    # run is the race this exists to lose. A claim that is never followed by a
    # conclusion strands nothing — the polling floor does not consult it.
    # Stamped when a partner's callback arrived and lost the claim — the
    # attempt that published the row holds it while it polls. The partner has
    # its 200 and will not call again, so whoever releases the claim collects
    # rather than leaving an answer that is already waiting to the floor.
    callback_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    callback_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    conversation_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # The partner's machine-readable reason, preserved rather than flattened
    # into free text (FR-029).
    partner_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many ``input-required`` round trips this exchange took. NOT a limit
    # — the run's existing budgets bound it (FR-048) — but the number that
    # makes turn-farming visible rather than merely bounded.
    continuation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    terminal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
