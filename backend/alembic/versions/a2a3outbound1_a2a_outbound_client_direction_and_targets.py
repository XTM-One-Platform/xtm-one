"""A2A outbound: direction, partner agents, and delegation records

What this creates:

* ``a2a_integration_settings.direction`` — the partner relationship's
  DIRECTION, inbound or outbound, never both. Nothing per-card lives on the
  settings row.
* ``a2a_client_targets`` — one row per partner agent this platform can task,
  with that agent's verdict, pinned origin, negotiated version, BINDING,
  fingerprints and circuit. One agent being unreachable must not pause
  delegation to its siblings, which a partner-wide circuit would do.
* ``a2a_delegations`` — work sent to a partner, including WHICH of its agents
  (``target_id``, ``SET NULL``: removing a target must not delete the history
  of work already sent through it).

Chained on ``d4und0173``, main's head. The A2A state it starts from is the
one ``a2a2intmodel1`` left — no revision between the two touches A2A.

**The backfill is the load-bearing part.** Every A2A integration that exists
at ``a2a2intmodel1`` serves inbound, because outbound did not exist before
this revision. It is stamped ``inbound`` explicitly rather than left to a
default, because the exposure hook decides exposure from direction: a row
without one is treated as neither direction and stops exposing the agents it
was binding. Skipping it would silently un-expose every live partner
integration on upgrade. The fail-closed missing-row rule is a backstop
against a bug, not a substitute for this.

And it runs whether or not the tables already exist. On an existing install
they do: ``run_migrations`` calls ``Base.metadata.create_all`` before
``alembic upgrade``, so this body finds all three present and empty. Table
existence gates the DDL below, table by table, and nothing else.

Lock profile: every foreign key here takes a lock on the table it REFERENCES
— ``CREATE TABLE`` locks nothing when the table does not exist yet, but its
foreign keys do — and ``integrations``, ``agents``, ``users`` and
``assignment_runs`` are all hot. The canonical recipe is followed below.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a2a3outbound1"
down_revision: str | Sequence[str] | None = "d4und0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS = "a2a_integration_settings"
_TARGETS = "a2a_client_targets"
_DELEGATIONS = "a2a_delegations"

#: Tables whose locks the foreign keys below will take.
_PARENTS = ("integrations", "agents", "users", "assignment_runs")

# Spelled as SQL literals rather than imported from ``app.a2a.providers``.
# A migration must keep meaning what it meant on the day it ran: importing a
# constant would let a later edit change what THIS revision did to a database
# it has already touched. The same reasoning is recorded on ``A2A_PROVIDERS``
# for the predicate in ``a2a2intmodel1`` — and it is why ``google_a2a`` is
# absent here despite having since become inbound-capable: no integration of
# that provider can exist in a database this revision has yet to touch.
_INBOUND_PROVIDERS_AT_THIS_REVISION = ("oauth_a2a", "entra_a2a")


def upgrade() -> None:
    conn = op.get_bind()
    existing = set(sa.inspect(conn).get_table_names())

    # Tables are created only where missing; on every path that reaches here
    # (fresh install, CI re-run, existing install) they may already exist.
    # The backfill below runs regardless.
    if {_SETTINGS, _TARGETS, _DELEGATIONS} - existing:
        # A long-lived transaction that has merely READ one of these blocks
        # the FK creation, and without a timeout that block queues every
        # subsequent statement behind it. Taken only when there is DDL to run.
        for parent in _PARENTS:
            terminate_lock_holders(conn, parent)
        set_lock_timeout(conn)

    if _SETTINGS not in existing:
        op.create_table(
            _SETTINGS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "integration_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("integrations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("direction", sa.String(8), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        # ONE unique index, named and shaped exactly as the model's
        # ``unique=True, index=True`` makes ``create_all`` build it — that
        # pair folds into a single UNIQUE index rather than a constraint plus
        # an index, and declaring both would give an upgraded deployment two
        # indexes on one column where a fresh install has one.
        op.create_index(
            "ix_a2a_integration_settings_integration_id",
            _SETTINGS,
            ["integration_id"],
            unique=True,
        )

    # Before the delegation table, which references it.
    if _TARGETS not in existing:
        op.create_table(
            _TARGETS,
            sa.Column(
                "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
            ),
            sa.Column(
                "integration_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("integrations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # NOT NULL: this row IS a card. A nullable column would also defeat
            # the uniqueness below, since PostgreSQL treats NULLs as distinct.
            sa.Column("card_url", sa.Text(), nullable=False),
            # Where work is SENT, as opposed to where the card was FETCHED.
            sa.Column("invocation_url", sa.Text(), nullable=True),
            sa.Column("pinned_origin", sa.Text(), nullable=True),
            sa.Column("agreed_protocol_version", sa.String(16), nullable=True),
            # Which A2A binding ``invocation_url`` speaks - JSONRPC or HTTP+JSON.
            # Beside the endpoint because it is the same decision: a card
            # declares a SET of interfaces and verification picks one.
            sa.Column("transport", sa.String(16), nullable=True),
            sa.Column("card_fingerprint", sa.String(64), nullable=True),
            sa.Column("accepted_card_fingerprint", sa.String(64), nullable=True),
            sa.Column("signature_state", sa.String(16), nullable=True),
            # Fail-closed: a row exists before verification, so an unverified
            # target is unusable rather than usable-by-omission.
            sa.Column(
                "usability", sa.String(16), nullable=False, server_default="unusable"
            ),
            sa.Column("unusable_reason", sa.String(48), nullable=True),
            sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_verification_error", sa.Text(), nullable=True),
            sa.Column(
                "consecutive_transient_failures",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_kind", sa.String(16), nullable=True),
            sa.Column("declared_name", sa.Text(), nullable=True),
            sa.Column("declared_description", sa.Text(), nullable=True),
            sa.Column("declared_skills", postgresql.JSON(), nullable=True),
            sa.Column("declared_input_types", postgresql.JSON(), nullable=True),
            sa.Column("declared_output_types", postgresql.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            # Where this partner said it lists the agents our credential
            # reaches, from the extension its card declares. NULL means the
            # card carried no such declaration.
            sa.Column("partner_discovery_path", sa.Text(), nullable=True),
            # One row per card per partner. Two rows for one card would verify
            # the same agent twice and give a calling model two tools that do
            # one thing.
            sa.UniqueConstraint("integration_id", "card_url", name="uq_a2a_target_card"),
        )
        op.create_index(
            "ix_a2a_client_targets_integration_id", _TARGETS, ["integration_id"]
        )
        # What the sweep scans. ``NULLS FIRST`` matches ``due_targets``'s
        # ORDER BY: PostgreSQL's default btree ordering is NULLS LAST, which
        # cannot serve that ordering in either scan direction, so a plain index
        # would be maintained on every write and never used. No partial
        # predicate: a target row is outbound by construction.
        op.create_index(
            "ix_a2a_client_targets_due",
            _TARGETS,
            [sa.text("last_verified_at ASC NULLS FIRST")],
        )

    if _DELEGATIONS not in existing:
        op.create_table(
            _DELEGATIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "integration_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("integrations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_ref",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("assignment_runs.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("partner_task_id", sa.Text(), nullable=True),
            sa.Column("protocol_version", sa.String(16), nullable=False),
            sa.Column("signature_state", sa.String(16), nullable=False),
            sa.Column("outcome", sa.String(32), nullable=True),
            sa.Column("failure_kind", sa.String(16), nullable=True),
            sa.Column("partner_reason", sa.Text(), nullable=True),
            sa.Column(
                "continuation_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "target_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_TARGETS}.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_a2a_delegations_integration_time",
            _DELEGATIONS,
            ["integration_id", "submitted_at"],
        )
        op.create_index("ix_a2a_delegations_run", _DELEGATIONS, ["run_ref"])
        op.create_index("ix_a2a_delegations_target_id", _DELEGATIONS, ["target_id"])

    # Unconditional, and idempotent by construction: NOT EXISTS means a
    # re-run inserts nothing.
    op.execute(
        sa.text(
            """
            INSERT INTO a2a_integration_settings
                (id, integration_id, direction, created_at, updated_at)
            SELECT gen_random_uuid(), i.id, 'inbound', NOW(), NOW()
            FROM integrations i
            WHERE i.provider = ANY(:providers)
              AND NOT EXISTS (
                  SELECT 1 FROM a2a_integration_settings s
                  WHERE s.integration_id = i.id
              )
            """
        ).bindparams(providers=list(_INBOUND_PROVIDERS_AT_THIS_REVISION))
    )


def downgrade() -> None:
    conn = op.get_bind()
    existing = set(sa.inspect(conn).get_table_names())
    if not ({_SETTINGS, _TARGETS, _DELEGATIONS} & existing):
        return

    # Direction lives only in the table dropped below, and ``upgrade()`` stamps
    # every settings-less integration ``inbound`` — so downgrade-then-upgrade
    # turns an outbound partner inbound and the exposure hook starts publishing
    # cards for its bound agents, the change ``change_direction`` refuses while
    # bound. Inbound-only rows reconstruct exactly and pass.
    if _SETTINGS in existing:
        outbound = conn.execute(
            sa.text(
                f"SELECT count(*) FROM {_SETTINGS} WHERE direction = 'outbound'"  # noqa: S608 - module constant, not input
            )
        ).scalar_one()
        if outbound:
            raise RuntimeError(
                f"Refusing to downgrade: {outbound} A2A registration(s) are "
                "outbound, and direction is stored only in the table this "
                "downgrade drops. Re-upgrading would stamp them 'inbound' and "
                "start exposing their bound agents. Set them to inbound, or "
                "delete them, before downgrading."
            )

    # Dropping a table drops its FK constraints, locking the referenced tables
    # exactly as creating them did.
    for parent in _PARENTS:
        terminate_lock_holders(conn, parent)
    set_lock_timeout(conn)

    # Children first: delegations reference targets.
    if _DELEGATIONS in existing:
        op.drop_table(_DELEGATIONS)
    if _TARGETS in existing:
        op.drop_table(_TARGETS)
    if _SETTINGS in existing:
        op.drop_table(_SETTINGS)
