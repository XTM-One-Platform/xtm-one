# SPDX-License-Identifier: Apache-2.0

"""Runner models – native, pull-based cross-platform OS-control agents.

A **Runner** is a native daemon (Windows / macOS / Linux) a user downloads,
enrolls once, and leaves running in their desktop session. It connects
*outbound* to XTM One (WebSocket, HTTP long-poll fallback) and *pulls*
commands — the platform never opens an inbound port on the machine. Agents
drive it through the ``computer_use`` built-in tools to capture genuine
full-desktop screenshots (audit evidence), control real authenticated
browsers, run shell commands, and read/write files.

Three tables live here:

* :class:`Runner` — one row per enrolled machine (first-class entity, NOT an
  integration). Three-tier visibility (own + group-shared + company-managed),
  a hashed ``run-`` credential (mirrors :class:`~app.models.api_key.UserApiKey`),
  a per-runner ``permission_mode`` + capability allow-list, and the
  ``host_facts`` the runner reports on connect (OS build, shells + versions,
  installed/authenticated browsers, monitor topology) so an agent can plan
  without probing.
* :class:`RunnerCommand` — the durable command queue. Every dispatched command
  is a row (reliability backstop for the event-driven WS fast-path) and the
  immutable audit trail of what an agent asked a machine to do.
* :class:`RunnerEnrollmentKey` — a hashed ``renr-`` enrollment credential. A
  single-use key (``max_uses = 1``) provisions one machine; a reusable key
  (``max_uses`` NULL / > 1) provisions a whole fleet via a script / MDM.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# ── Canonical vocabularies (single source of truth) ─────────────────────────

# Credential prefixes. ``run-`` authenticates a runner on every call after
# enrollment; ``renr-`` is the short-lived / reusable enrollment credential a
# runner presents ONCE to ``POST /runners/enroll`` in exchange for a ``run-``.
RUNNER_KEY_PREFIX = "run-"
ENROLLMENT_KEY_PREFIX = "renr-"

# Platforms a runner can report. Kept intentionally small — the OS family, not
# the exact distro (that lives in ``host_facts``).
RUNNER_PLATFORMS: tuple[str, ...] = ("windows", "macos", "linux")

# Capability ids an agent can be granted per runner. ``host_facts`` is implicit
# (always allowed — it carries no side effect) and deliberately NOT listed.
RUNNER_CAPABILITIES: tuple[str, ...] = (
    "screenshot",  # full-desktop capture (the audit-evidence primary use case)
    "computer_action",  # mouse / keyboard synthesis (move, click, drag, scroll, type, key)
    "run_shell",  # execute a shell command
    "file_ops",  # read_file / write_file / list_dir
    "browser_control",  # drive a real, authenticated local browser
)

# Command kind -> capability it requires (host_facts requires none).
COMMAND_KIND_CAPABILITY: dict[str, str] = {
    "host_facts": "",
    "screenshot": "screenshot",
    "computer_action": "computer_action",
    "run_shell": "run_shell",
    # Read, list or stop the background jobs a run_shell started.
    "shell_job": "run_shell",
    "read_file": "file_ops",
    "write_file": "file_ops",
    "list_dir": "file_ops",
    "browser_control": "browser_control",
}

# Permission modes. ``restricted`` (default) = least privilege: only the
# capabilities in the allow-list, optionally scoped further (shell command /
# path allow-lists) and optionally gated by human approval. ``full_control`` =
# the deliberate opt-in the user asked for: every capability, every command, no
# per-op approval (warned + audited in the UI, capability list ignored).
PERMISSION_MODE_RESTRICTED = "restricted"
PERMISSION_MODE_FULL_CONTROL = "full_control"
RUNNER_PERMISSION_MODES: tuple[str, ...] = (
    PERMISSION_MODE_RESTRICTED,
    PERMISSION_MODE_FULL_CONTROL,
)

# Terminal states of a command row.
COMMAND_TERMINAL_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "timeout",
)

# Terminal states a RUNNER may report on a result upload. ``cancelled`` is a
# server-side decision (approval rejected / revoked), never runner-reported, so
# it is excluded: a result carrying an out-of-set status is not a legitimate
# runner report and is refused rather than silently coerced to "completed"
# (which would file a malformed / newer runner's output as valid evidence).
# Mirrors the Rust ``OutcomeStatus`` enum in ``apps/runner/src/protocol.rs``.
RUNNER_RESULT_STATUSES: tuple[str, ...] = ("completed", "failed", "timeout")


class Runner(Base):
    """One enrolled machine. First-class entity, three-tier visibility.

    The ``run-`` credential is stored as a bcrypt hash + short display prefix
    exactly like :class:`~app.models.api_key.UserApiKey`; the raw key is shown
    only once (at enrollment) and never persisted in the clear.
    """

    __tablename__ = "runners"
    __table_args__ = (
        Index("ix_runners_is_company_managed", "is_company_managed"),
        # The runner authenticates with its key on every WS frame / long-poll;
        # the prefix column is the indexed narrowing lookup before the bcrypt
        # compare, mirroring the API-key resolver.
        Index("ix_runners_key_prefix", "key_prefix"),
        # Enrollment looks up the machine's existing row to converge on it.
        Index("ix_runners_machine_id", "machine_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NULL when company-managed (shared with all users; admin-only edit).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Human-friendly label shown in the UI (defaults to the reported hostname).
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # OS family: "windows" | "macos" | "linux".
    platform: Mapped[str] = mapped_column(String(20), nullable=False)

    # Reported hostname (display only — NOT a machine identity: cloned VM
    # fleets legitimately share one).
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Stable machine identifier the runner reports at enrollment: a salted
    # SHA-256 of the OS machine id (/etc/machine-id, IOPlatformUUID,
    # MachineGuid) — opaque to the server, never the raw id. THE dedup key:
    # a re-enrollment from the same machine converges on this row instead of
    # duplicating it. NULL for runners enrolled by builds that predate it.
    machine_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Credential (mirror UserApiKey) ---
    # First 12 chars of the key for display: "run-xxxxxxxx...".
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    # bcrypt hash of the full ``run-`` key.
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Permission model ---
    permission_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PERMISSION_MODE_RESTRICTED
    )
    # Enabled capability ids (see ``RUNNER_CAPABILITIES``). Honored only in
    # ``restricted`` mode; ``full_control`` allows everything regardless.
    capabilities: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    # Restricted-mode scoping (all optional, all NULL = no extra restriction):
    #   allowed_shell_commands — standing approvals: a matching shell command
    #                            runs immediately, never queued for approval.
    #                            With require_approval OFF it is also the hard
    #                            scope (only listed commands run at all).
    #   blocked_shell_commands — standing refusals: a matching shell command
    #                            is always refused, never even offered for
    #                            approval. Wins over the allow-list.
    #   allowed_paths          — allow-list of path prefixes for file ops.
    #   require_approval       — when True, SHELL commands not covered by the
    #                            allow/block lists wait for explicit human
    #                            approval (HITL) before dispatch. Approval is
    #                            scoped to shell only: every other capability
    #                            is an enable-once toggle.
    allowed_shell_commands: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    blocked_shell_commands: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    allowed_paths: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- Fleet targeting ---
    # Free-form labels an agent's runner selector can match on (e.g.
    # "audit", "eu-west", "workstation"). Distinct from ``tags`` (org/filter).
    labels: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # --- Reported host context (updated on connect / heartbeat) ---
    # {os, os_version, os_build, arch, shells:{powershell:"5.1",pwsh:"7.4",bash:...},
    #  browsers:[{name,version,authenticated}], monitors:[{w,h,primary}], ...}
    host_facts: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Version string of the running runner binary (MAJOR.YYMMDD.SEQ).
    agent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # --- Liveness ---
    # ``last_seen_at`` recency is the authoritative online/offline signal
    # (multi-replica safe — no shared in-process WS registry needed). The
    # ``status`` column is the presence cache the flow events follow: set
    # ``online`` by enrollment and by the heartbeats of an active runner,
    # ``offline`` by the stale-heartbeat sweep
    # (``runner_transport.sweep_stale_runners``), ``revoked`` (final) by the
    # revoke route - never by a socket closing.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="offline"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Transport the runner last connected over: "ws" | "long_poll".
    transport: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Instant revocation: flip to False to reject the credential immediately.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Three-tier visibility (mirror MCPServer) ---
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    # Required, not optional: that helper is generic over every entity carrying
    # ``shared_group_ids`` and references these columns unconditionally, so a
    # shared entity without them raises at query-build time.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # --- Usage tracking ---
    command_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunnerCommand(Base):
    """A single command dispatched to a runner.

    The durable backstop behind the event-driven WebSocket fast-path AND the
    immutable audit record of every OS-level action an agent requested. Claimed
    atomically by exactly one transport (WS or long-poll) via a ``pending`` ->
    ``dispatched`` transition; the runner posts a terminal result which wakes
    the awaiting tool through Redis pub/sub.
    """

    __tablename__ = "runner_commands"
    __table_args__ = (
        # The transport claims the next ``pending`` command for a runner; the
        # awaiting tool polls a single command by id as the pub/sub backstop.
        Index("ix_runner_commands_runner_status", "runner_id", "status"),
        Index("ix_runner_commands_created_at", "created_at"),
        # The scheduler's sweep of given-up commands past their lease
        # (``runner_transport.expire_given_up_commands``) runs every
        # tick across all runners; partial, so the audit history is not in it.
        Index(
            "ix_runner_commands_given_up_dispatched",
            "dispatched_at",
            postgresql_where=text("status = 'dispatched' AND given_up_at IS NOT NULL"),
        ),
        # A cancelled or dead-lettered flow run gives up its open commands
        # (``runner_transport.give_up_flow_run_commands``); partial, so
        # finished commands - the history - are not in it.
        Index(
            "ix_runner_commands_flow_run_open",
            "flow_run_id",
            postgresql_where=text(
                "flow_run_id IS NOT NULL "
                "AND status IN ('pending', 'pending_approval', 'dispatched')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who dispatched it (audit). SET NULL preserves the commands a deleted user
    # dispatched on runners that OUTLIVE them (company-managed / group-shared
    # machines). Commands on the user's OWN runners are removed with those
    # runners: Runner.user_id cascades on user deletion by design — a departed
    # user's private enrollment + credential must die, and NULLing it instead
    # is not an option because a NULL owner means company-managed. Retaining
    # history across owner/runner deletion (soft-delete / archive) is the
    # tracked retention follow-up.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Which agent / run context dispatched it (audit + scoping). Plain UUIDs,
    # no FK: they reference several tables and must survive their deletion.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Set when a flow's `Runner command` node dispatched it: audit
    # trail plus the join key for the node's wait-mode park.
    flow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Command kind (see ``COMMAND_KIND_CAPABILITY``) + its structured args.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    # Structured result the runner returned (text output, exit_code, stdout,
    # host_facts, listing, ...). Binary results (screenshots) land in a
    # ``FileAttachment`` referenced by ``result_file_id`` instead.
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    result_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_attachments.id", ondelete="SET NULL"),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When whoever dispatched it stopped waiting (the agent tool's wait ended,
    # its chat turn was cancelled, its flow run ended), recorded on the row so
    # it outlives the waiter's process: a command nobody waits for is never
    # requeued or redelivered (``runner_transport.give_up_on_command``).
    given_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunnerEnrollmentKey(Base):
    """A hashed ``renr-`` enrollment credential.

    Presented ONCE by a runner to ``POST /runners/enroll`` in exchange for a
    durable ``run-`` credential + a freshly-created :class:`Runner` row. The
    defaults carried here (labels, capabilities, permission_mode) seed the new
    runner so an unattended MDM roll-out lands fully configured.
    """

    __tablename__ = "runner_enrollment_keys"
    __table_args__ = (
        Index("ix_runner_enrollment_keys_key_prefix", "key_prefix"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL = unlimited reusable key (fleet provisioning). 1 = single-use.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Defaults stamped onto every Runner enrolled with this key.
    default_labels: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    default_capabilities: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    default_permission_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PERMISSION_MODE_RESTRICTED
    )
    # Seeds ``Runner.require_approval``: true when the person creating
    # the key turned on "Shell commands need approval", so the machines it
    # enrolls hold shell commands for approval in the chat. No preset implies
    # it: the XTM One Desktop preset enrolls a machine with every
    # capability and this switch off unless turned on. NULL / false = no
    # approval gate unless the operator adds one.
    default_require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # When set, enrolled runners become company-managed (admin fleet).
    enroll_as_company_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
