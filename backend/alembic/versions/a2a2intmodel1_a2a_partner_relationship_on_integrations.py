"""move the A2A partner relationship onto integrations

One revision for one change of shape. An A2A partner is no longer a set of
columns on an exposure — it is an ``integrations`` row, and an agent opts in
by binding it through its ORDINARY ``integration_bindings`` (the Tools tab).
Exposure follows that binding rather than an explicit toggle.

Three steps, and the ORDER between them is load-bearing:

1. **Federation pins move out of ``a2a_exposures``.** The three per-exposure
   columns added by ``a2a1srv0001`` (``entra_tenant_id`` / ``entra_client_id``
   / ``entra_audience``) are backfilled into ``entra_a2a`` integrations, merged
   into each agent's bindings, then dropped. No A2A-specific federation schema
   remains; the auth path resolves pins per request from the agent's bindings
   (``app.a2a.federation``).
2. **``mcp_oauth_clients`` gains ``a2a_integration_id``**, so a partner
   credential can belong to a partner rather than to one agent.
3. **Exposures binding no A2A integration are revoked**, converging rows the
   old explicit toggle created against the new "exposed == bound" invariant.

Step 3 MUST come after step 1: it revokes on the absence of a binding, and
step 1 is what gives a migrated federation its binding. Reversed, this
migration would tear down exactly the live Foundry setups step 1 exists to
preserve. They were briefly three separate revisions and are merged here so
that ordering is a property of the file rather than of a chain someone could
re-point while rebasing.

**Backfill covers live exposures only.** A live exposure with all three pins
gets an ``entra_a2a`` integration created from them (extra config
Fernet-encrypted exactly as the API stores it) and merged into its agent's
bindings under the ``entra_a2a`` key — a working federation, e.g. the first
live Foundry setup, must survive this upgrade with no partner 401s and no
re-typing. Identical pin sets owned by the same user collapse into ONE
integration, which is the point of the rework. The integration is owned by the
exposure's ``exposed_by_user_id`` (falling back to ``run_as_user_id`` when the
exposer was deleted) because that is who demonstrated authority over the
config when it was written. Revoked exposures are NOT backfilled: they can
never resolve again, so their pins have no future use — the audit log keeps
the historical record.

Nothing backfills step 2. An exposure-scoped credential is correctly scoped
already, and inventing an integration for it would invent a partner
relationship nobody configured. Both columns stay nullable and both stay
honoured by the auth path: ``a2a_exposure_id`` rows are credentials minted by
the older explicit-toggle flow and are still in partners' hands.

Earlier, never-pushed cuts of this work left two artifacts on development
databases — an ``a2a_exposures.entra_integration_id`` column, and the split
revision ids ``a2a2fedint1`` / ``a2a3oauthint`` / ``a2a4converge01``. The
guarded drops below converge the former; the latter need a re-stamp onto
this revision's parent before it runs (``alembic stamp w3ks3097003``, or
whatever ``down_revision`` names by then), and are no-ops everywhere else
since every step here is guarded on the state it changes.

Rolling-deploy caveat, accepted: replicas still running the previous version
read the three legacy columns, so between this migration and their restart
they refuse Entra credentials (SELECTs naming dropped columns error, folding
into the task surface's uniform refusal). Fail-closed, self-heals on restart,
and the OAuth credential path is untouched throughout — no data is at risk,
availability of ONE credential form degrades briefly. The alternative (keep
writing both shapes) would leave two sources of trust that can disagree.

Revision ID: a2a2intmodel1
Revises: w3ks3097003
Create Date: 2026-08-25 12:00:00.000000
"""

import json
import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders
from app.core.utils import sanitize_json_strings

revision: str = "a2a2intmodel1"
down_revision: str | Sequence[str] | None = "w3ks3097003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

# Two diagnostic channels, deliberately: ``print`` for the handful of summaries
# an operator running the upgrade must see in its output (what was revoked,
# what deduplication skipped), and ``logger.warning`` for per-row detail they
# only need when chasing one of those summaries. Both surface under Alembic;
# the split is about which is worth reading on a green run.

#: Mirrors ``app.a2a.providers`` — inlined because a migration must stay
#: readable years after the module may have moved.
_ENTRA_PROVIDER = "entra_a2a"
_LEGACY_COLUMNS = ("entra_tenant_id", "entra_client_id", "entra_audience")

# Artifacts of an earlier, never-pushed cut (see docstring).
_OLD_CUT_FK = "a2a_exposures_entra_integration_id_fkey"
_OLD_CUT_INDEX = "ix_a2a_exposures_entra_integration_id"

#: How many existing federation integrations the backfill will decrypt to seed
#: its deduplication map. One row per partner relationship is the real shape,
#: so this is a backstop against an unbounded decrypt inside the lock window
#: rather than a limit anyone should reach.
_MAX_DEDUP_SEED_ROWS = 500

_CLIENTS_TABLE = "mcp_oauth_clients"
_CLIENTS_COLUMN = "a2a_integration_id"
_CLIENTS_FK = "mcp_oauth_clients_a2a_integration_id_fkey"
_CLIENTS_INDEX = "ix_mcp_oauth_clients_a2a_integration_id"

#: An agent counts as bound only when a key holds a NON-EMPTY array —
#: unbinding through the API usually leaves ``{"oauth_a2a": []}`` rather than
#: removing the key, and mere key presence would read that as still exposed.
#: ``jsonb_typeof`` guards ``jsonb_array_length`` against a malformed value
#: (it raises on non-arrays, which would abort the whole migration).
#: The provider keys are spelled literally, not interpolated: a constant is
#: what makes this predicate provably free of input.
_BINDS_A2A = """
    EXISTS (
        SELECT 1
        FROM jsonb_each(COALESCE(a.integration_bindings::jsonb, '{}'::jsonb)) kv
        WHERE kv.key IN ('oauth_a2a', 'entra_a2a')
          AND jsonb_typeof(kv.value) = 'array'
          AND jsonb_array_length(kv.value) > 0
    )
"""


#: Whether PostgreSQL refuses to de-escape the agent's bindings: a ``json``
#: column keeps a lone UTF-16 surrogate or ``\\u0000`` escape verbatim, and the
#: ``::jsonb`` of ``_BINDS_A2A`` refused such a row, aborting the upgrade.
#: The test of ``app.core.json_sql.json_unparseable``, spelled here
#: for the same reason as the provider keys; its patterns are the
#: ``_UNREADABLE_PARAMS``. Such bindings are repaired before step 3 reads
#: them (``_repair_unreadable_bindings``).
_BINDINGS_UNREADABLE = """
    ((CAST(a.integration_bindings AS text) ~ :refused_escape
      AND regexp_replace(replace(CAST(a.integration_bindings AS text), :escaped_backslash, '_'),
                         :surrogate_pair, '_', 'g') ~ :refused_escape)
     OR CAST(a.integration_bindings AS text) ~ :out_of_range_number)
"""

_UNREADABLE_PARAMS = {
    "refused_escape": r"\\u(0000|[dD][89a-fA-F])",
    "surrogate_pair": r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}",
    "escaped_backslash": "\\\\",
    "out_of_range_number": (
        r"(^|[:,\[])\s*-?([0-9]+(\.[0-9]+)?[eE][+-]?[0-9]{5}"
        r"|([0-9]+\.)?[0-9]{250}[0-9]{250}[0-9]{250}[0-9]{250})"
    ),
}


def _castable(conn, stored: str) -> bool:
    """Whether PostgreSQL casts *stored* to ``jsonb``: the exact answer, in a savepoint."""
    try:
        with conn.begin_nested():
            # IS NOT NULL: the value itself is never decoded by the driver.
            conn.execute(sa.text("SELECT CAST(CAST(:v AS json) AS jsonb) IS NOT NULL"), {"v": stored})
    except sa.exc.DBAPIError:
        return False
    return True


def _repaired_json(stored: str) -> str | None:
    """*stored* as the write path stores it now (``sanitize_json_strings``), ``None`` when unreadable."""
    try:
        return json.dumps(sanitize_json_strings(json.loads(stored)), allow_nan=False)
    except (ValueError, RecursionError):
        return None


def _repair_unreadable_bindings(conn) -> tuple[list[str], list[str]]:
    """Rewrite the unreadable bindings of agents with a live exposure, so step 3 reads them.

    ``_BINDINGS_UNREADABLE`` picks the candidates and a real cast decides each:
    bindings that cast are left alone. Lone surrogates become U+FFFD and
    ``\\u0000`` goes, exactly as every JSON write now stores them, and the
    revocation below then decides on the repaired value like on any other.
    Returns ``(revoke, left_out)``: the agents with a live exposure whose
    bindings cannot be cast even so - revoked, nothing shows their binding -
    and the agents without one, which the revocation may still evaluate before
    its join and must leave out.
    """
    rows = conn.execute(
        sa.text(
            f"""
            SELECT a.id, CAST(a.integration_bindings AS text),
                   EXISTS (SELECT 1 FROM a2a_exposures e WHERE e.agent_id = a.id AND e.revoked_at IS NULL)
              FROM agents a
             WHERE {_BINDINGS_UNREADABLE}
            """  # noqa: S608 - no input: the predicate is a module constant
        ),
        _UNREADABLE_PARAMS,
    ).all()
    repaired, revoke, left_out = 0, [], []
    for agent_id, stored, exposed in rows:
        if _castable(conn, stored):
            continue
        if not exposed:
            left_out.append(str(agent_id))
            continue
        clean = _repaired_json(stored)
        if clean is None or not _castable(conn, clean):
            revoke.append(str(agent_id))
            continue
        conn.execute(
            sa.text("UPDATE agents SET integration_bindings = CAST(:b AS json) WHERE id = :id"),
            {"b": clean, "id": agent_id},
        )
        repaired += 1
    if repaired:
        print(  # noqa: T201 - migrations report through stdout
            f"a2a: repaired the integration bindings of {repaired} agent(s) holding a lone "
            "surrogate or NUL escape (stored as U+FFFD, as every JSON write now stores them)"
        )
    if revoke:
        print(  # noqa: T201 - migrations report through stdout
            "a2a: revoking the live exposure of agent(s) whose integration bindings "
            f"cannot be read ({', '.join(sorted(revoke))})"
        )
    return revoke, left_out


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
    )


def _merge_agent_binding(conn, agent_id, integration_id: str) -> None:
    """Append *integration_id* to the agent's ``entra_a2a`` bindings."""
    bindings = (
        conn.execute(
            sa.text("SELECT integration_bindings FROM agents WHERE id = :id"),
            {"id": str(agent_id)},
        ).scalar()
        or {}
    )
    if isinstance(bindings, str):  # driver-dependent JSON decoding
        bindings = json.loads(bindings)
    bound = list(bindings.get(_ENTRA_PROVIDER) or [])
    if integration_id in bound:
        return
    bound.append(integration_id)
    bindings[_ENTRA_PROVIDER] = bound
    conn.execute(
        sa.text(
            "UPDATE agents SET integration_bindings = CAST(:b AS json) "
            "WHERE id = :id"
        ),
        {"b": json.dumps(bindings), "id": str(agent_id)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — federation pins move out of a2a_exposures
# ─────────────────────────────────────────────────────────────────────────────


def _move_entra_pins_to_integrations(conn) -> None:
    # Converge the earlier cut's per-exposure binding column (dev-only).
    if _has_column(conn, "a2a_exposures", "entra_integration_id"):
        terminate_lock_holders(conn, "a2a_exposures")
        set_lock_timeout(conn)
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {_OLD_CUT_INDEX}"))
        conn.execute(
            sa.text(
                f"ALTER TABLE a2a_exposures DROP CONSTRAINT IF EXISTS {_OLD_CUT_FK}"
            )
        )
        conn.execute(
            sa.text(
                "ALTER TABLE a2a_exposures "
                "DROP COLUMN IF EXISTS entra_integration_id"
            )
        )

    # Backfill and drop both run only while the legacy columns still exist, so
    # a re-run (CI's idempotency job, a crashed replica retrying) is a no-op.
    if not all(_has_column(conn, "a2a_exposures", c) for c in _LEGACY_COLUMNS):
        return

    # Imported here, not at module scope: integrity tests import migration
    # modules offline, and the crypto helper pulls in app settings.
    from app.core.security import decrypt_json, encrypt_value

    # Seed the dedup map from ``entra_a2a`` integrations that ALREADY exist,
    # keyed exactly as the backfill keys new ones. Two cases need this and
    # both are real: a downgrade leaves these rows in place by design, so a
    # re-upgrade would otherwise mint a second integration with identical pins
    # and append a second binding; and an operator who configured the partner
    # by hand before upgrading would get a duplicate beside their own.
    #
    # Fernet is not deterministic, so matching means decrypting. That is one
    # row per partner relationship today, but the shape is not enforced by
    # anything, and this runs inside the migration's lock window — so it is
    # capped rather than trusted. Past the cap the pass still converges; it
    # just may mint a duplicate integration instead of reusing one, which an
    # operator can merge afterwards. Saying so is the point of the warning:
    # silence would read as "deduplicated everything".
    #
    # An unreadable row is skipped rather than fatal: it simply does not
    # participate in dedup.
    seen_rows = 0
    created: dict[tuple, str] = {}
    for existing in (
        conn.execute(
            sa.text(
                "SELECT id, user_id, extra_config_enc FROM integrations "
                "WHERE provider = :p ORDER BY created_at LIMIT :lim"
            ),
            {"p": _ENTRA_PROVIDER, "lim": _MAX_DEDUP_SEED_ROWS + 1},
        )
        .mappings()
        .all()
    ):
        seen_rows += 1
        if seen_rows > _MAX_DEDUP_SEED_ROWS:
            print(  # noqa: T201 - migrations report through stdout
                f"a2a: more than {_MAX_DEDUP_SEED_ROWS} existing "
                f"{_ENTRA_PROVIDER} integrations; deduplication considered "
                "only the oldest, so the backfill may create duplicates"
            )
            break
        try:
            extra = decrypt_json(existing["extra_config_enc"]) or {}
            key = (
                str(existing["user_id"]),
                str(extra["tenant_id"]).lower(),
                str(extra["client_id"]).lower(),
                extra["audience"],
            )
        except Exception:
            logger.warning(
                "Backfill: existing %s integration %s has no readable pins; "
                "it will not participate in deduplication",
                _ENTRA_PROVIDER,
                existing["id"],
            )
            continue
        created.setdefault(key, str(existing["id"]))

    rows = (
        conn.execute(
            sa.text(
                "SELECT id, agent_id, entra_tenant_id, entra_client_id, "
                "       entra_audience, exposed_by_user_id, run_as_user_id "
                "FROM a2a_exposures "
                "WHERE revoked_at IS NULL "
                "  AND entra_tenant_id IS NOT NULL "
                "  AND entra_client_id IS NOT NULL "
                "  AND entra_audience IS NOT NULL"
            )
        )
        .mappings()
        .all()
    )

    for row in rows:
        owner = row["exposed_by_user_id"] or row["run_as_user_id"]
        key = (
            str(owner),
            str(row["entra_tenant_id"]).lower(),
            str(row["entra_client_id"]).lower(),
            row["entra_audience"],
        )
        integration_id = created.get(key)
        if integration_id is None:
            integration_id = str(uuid.uuid4())
            extra = json.dumps(
                {
                    "tenant_id": str(row["entra_tenant_id"]),
                    "client_id": str(row["entra_client_id"]),
                    "audience": row["entra_audience"],
                }
            )
            conn.execute(
                sa.text(
                    "INSERT INTO integrations ("
                    "  id, user_id, is_company_managed, is_builtin, "
                    "  oauth_share_acknowledged, access_restricted, provider, "
                    "  auth_type, name, description, extra_config_enc, status, "
                    "  call_count, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :user_id, false, false, false, false, :provider, "
                    "  'none', :name, :description, :extra, 'connected', 0, "
                    "  now(), now()"
                    ")"
                ),
                {
                    "id": integration_id,
                    "user_id": str(owner),
                    "provider": _ENTRA_PROVIDER,
                    # The tenant prefix keeps two migrated partner configs
                    # tellable apart in a picker; nothing is unique on name.
                    "name": (
                        "Microsoft Entra A2A federation "
                        f"({str(row['entra_tenant_id'])[:8]}, migrated)"
                    ),
                    "description": (
                        "Created automatically when per-agent A2A Entra "
                        "federation pins moved into integrations. Bound to "
                        "the agent's integrations (Tools tab) as the "
                        "federation opt-in."
                    ),
                    "extra": encrypt_value(extra),
                },
            )
            created[key] = integration_id
        _merge_agent_binding(conn, row["agent_id"], integration_id)

    for legacy in _LEGACY_COLUMNS:
        terminate_lock_holders(conn, "a2a_exposures")
        set_lock_timeout(conn)
        conn.execute(
            sa.text(f"ALTER TABLE a2a_exposures DROP COLUMN IF EXISTS {legacy}")
        )


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — a partner credential belongs to a partner, not to one agent
# ─────────────────────────────────────────────────────────────────────────────


def _add_client_integration_column(conn) -> None:
    if not _has_column(conn, _CLIENTS_TABLE, _CLIENTS_COLUMN):
        # An EXISTING, live table: take the canonical lock-safe recipe so the
        # ALTER cannot queue behind a long-running reader during a rolling
        # deploy (backend/AGENTS.md > "Database migrations (safety)").
        terminate_lock_holders(conn, _CLIENTS_TABLE)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE {_CLIENTS_TABLE} "
                f"ADD COLUMN IF NOT EXISTS {_CLIENTS_COLUMN} UUID"
            )
        )

    # Guarded separately from the column: the startup safety net
    # (``_ensure_model_columns``) adds model columns WITHOUT constraints, so a
    # database can reach this point with the column present and the FK absent.
    if not _has_constraint(conn, _CLIENTS_FK):
        terminate_lock_holders(conn, _CLIENTS_TABLE)
        terminate_lock_holders(conn, "integrations")
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE {_CLIENTS_TABLE} ADD CONSTRAINT {_CLIENTS_FK} "
                f"FOREIGN KEY ({_CLIENTS_COLUMN}) REFERENCES integrations(id) "
                "ON DELETE CASCADE"
            )
        )

    conn.execute(
        sa.text(
            f"CREATE INDEX IF NOT EXISTS {_CLIENTS_INDEX} "
            f"ON {_CLIENTS_TABLE} ({_CLIENTS_COLUMN})"
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — converge exposures that predate "exposed == bound"
# ─────────────────────────────────────────────────────────────────────────────


def _revoke_unbound_exposures(conn) -> None:
    """Revoke live exposures whose agent binds no A2A integration.

    Rows created by the earlier explicit toggle predate the rule, so an agent
    could carry a LIVE exposure while binding nothing — reachable by a partner
    with no binding to show for it, and invisible in the UI, which now derives
    "exposed" from bindings.

    Exposures whose agent DOES bind one are left untouched: they are correct
    under the new rule, and re-minting their card token would break live
    partner integrations for no reason. That includes everything step 1 just
    migrated, which is why this runs last.

    Rows are revoked, never deleted — the retained row is what makes "a
    revoked card URL never resolves again" enforceable (see ``models/a2a``).

    Bindings PostgreSQL cannot read are repaired first and decided on like any
    other; bindings that cannot be read even so show no binding, and
    their exposure is revoked - the restrictive outcome, never a live exposure
    nobody can account for.
    """
    revoke, left_out = _repair_unreadable_bindings(conn)
    result = conn.execute(
        sa.text(
            f"""
            UPDATE a2a_exposures e
               SET revoked_at = now(),
                   revoked_reason = 'unbound',
                   updated_at = now()
              FROM agents a
             WHERE a.id = e.agent_id
               AND e.revoked_at IS NULL
               AND CASE WHEN CAST(a.id AS text) = ANY(CAST(:revoke AS text[])) THEN true
                        WHEN CAST(a.id AS text) = ANY(CAST(:left_out AS text[])) THEN false
                        ELSE NOT {_BINDS_A2A} END
            """  # noqa: S608 - no input: the predicate is a module constant
        ),
        {"revoke": revoke, "left_out": left_out},
    )
    if result.rowcount:
        # Worth an operator's attention: each row was a reachable agent whose
        # partner (if any) stops resolving at the next request.
        print(  # noqa: T201 - migrations report through stdout
            f"a2a: revoked {result.rowcount} exposure(s) with no A2A "
            "integration binding"
        )


def upgrade() -> None:
    conn = op.get_bind()

    # Fresh installs run create_all + stamp head — the tables already have the
    # final shape and every guard below no-ops. The a2a_exposures probe also
    # covers a deployment that never enabled the feature.
    if not (_has_table(conn, "a2a_exposures") and _has_table(conn, "agents")):
        return

    _move_entra_pins_to_integrations(conn)
    _add_client_integration_column(conn)
    _revoke_unbound_exposures(conn)


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "a2a_exposures"):
        return

    # Step 3 reverses to nothing, and that IS the correct reverse. No data was
    # destroyed: every exposure row is retained, only flagged revoked.
    # Re-livening them would put agents back on the internet under an exposure
    # nobody re-approved — a silent widening, and precisely what the revocation
    # machinery exists to prevent. An operator who genuinely wants one of these
    # agents reachable again binds an A2A integration to it, which mints a
    # fresh card URL and is an explicit, audited act.

    # Step 2 reverses with its rows. Credentials minted for an integration
    # cannot be re-scoped to an exposure (one integration spans many agents),
    # so they go with the column: leaving orphans would leave usable secrets
    # whose scope the old code cannot evaluate.
    if _has_column(conn, _CLIENTS_TABLE, _CLIENTS_COLUMN):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {_CLIENTS_INDEX}"))
        terminate_lock_holders(conn, _CLIENTS_TABLE)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE {_CLIENTS_TABLE} "
                f"DROP CONSTRAINT IF EXISTS {_CLIENTS_FK}"
            )
        )
        # Identifiers below are module constants, never input.
        conn.execute(
            sa.text(
                f"DELETE FROM {_CLIENTS_TABLE} "  # noqa: S608
                f"WHERE {_CLIENTS_COLUMN} IS NOT NULL"
            )
        )
        conn.execute(
            sa.text(
                f"ALTER TABLE {_CLIENTS_TABLE} "
                f"DROP COLUMN IF EXISTS {_CLIENTS_COLUMN}"
            )
        )

    # Step 1 rebuilds the prior shape: pins back onto the live exposure rows,
    # decrypted from the FIRST usable config bound to each exposure's agent
    # (the old shape can hold only one partner per exposure). The integration
    # rows and agent bindings are LEFT IN PLACE — users may have created or
    # shared them after the upgrade, the old code never reads them for
    # federation, and its tool building skips providers it does not know.
    for column, type_ in (
        ("entra_tenant_id", "UUID"),
        ("entra_client_id", "UUID"),
        ("entra_audience", "VARCHAR(200)"),
    ):
        if not _has_column(conn, "a2a_exposures", column):
            terminate_lock_holders(conn, "a2a_exposures")
            set_lock_timeout(conn)
            conn.execute(
                sa.text(
                    f"ALTER TABLE a2a_exposures "
                    f"ADD COLUMN IF NOT EXISTS {column} {type_}"
                )
            )

    from app.core.security import decrypt_json

    rows = (
        conn.execute(
            sa.text(
                "SELECT e.id, a.integration_bindings "
                "FROM a2a_exposures e JOIN agents a ON a.id = e.agent_id "
                "WHERE e.revoked_at IS NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        bindings = row["integration_bindings"] or {}
        if isinstance(bindings, str):
            bindings = json.loads(bindings)
        pins = None
        for integration_id in bindings.get(_ENTRA_PROVIDER) or []:
            enc = conn.execute(
                sa.text(
                    "SELECT extra_config_enc FROM integrations "
                    "WHERE id = :id AND provider = :p"
                ),
                {"id": str(integration_id), "p": _ENTRA_PROVIDER},
            ).scalar()
            try:
                extra = decrypt_json(enc) or {}
                pins = {
                    "tenant": str(uuid.UUID(str(extra["tenant_id"]).strip())),
                    "client": str(uuid.UUID(str(extra["client_id"]).strip())),
                    "audience": str(extra["audience"]).strip()[:200],
                }
                break
            except Exception:
                logger.warning(
                    "Downgrade: binding %s on exposure %s did not yield "
                    "usable pins; trying the next one",
                    integration_id,
                    row["id"],
                )
        if pins is None:
            continue
        conn.execute(
            sa.text(
                "UPDATE a2a_exposures SET "
                "  entra_tenant_id = :tenant, "
                "  entra_client_id = :client, "
                "  entra_audience = :audience "
                "WHERE id = :id"
            ),
            {**pins, "id": str(row["id"])},
        )
