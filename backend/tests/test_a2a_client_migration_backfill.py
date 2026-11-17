"""The backfill is what stops an upgrade silently un-exposing live partners.

Every A2A integration that exists today serves inbound, because outbound did
not exist until this revision. The exposure hook now decides exposure from
direction, and a row without a settings record belongs to neither — so an
upgrade that skipped the backfill would take every live partner integration
offline without a single error.

"The default handles it" is the tempting wrong answer: the fail-closed
missing-row rule is a backstop against a bug, not a substitute for stamping
what is already true.

The shape tests read the revision's source; ``TestItRunsOnAnExistingInstall``
runs its body against the state ``run_migrations`` actually hands it.
"""

from __future__ import annotations

import pathlib
import re
import uuid

import pytest

REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "a2a3outbound1_a2a_outbound_client_direction_and_targets.py"
)


@pytest.fixture(scope="module")
def source() -> str:
    return REVISION.read_text(encoding="utf-8")


class TestBackfillShape:
    def test_it_stamps_inbound_explicitly(self, source):
        assert "'inbound'" in source

    def test_it_covers_exactly_the_providers_that_existed(self, source):
        # Spelled as SQL literals, not imported. A migration must keep meaning
        # what it meant on the day it ran: importing the constant would let a
        # later edit change what THIS revision did to a database it already
        # touched. The same reasoning governs ``a2a2intmodel1``'s predicate.
        m = re.search(
            r"_INBOUND_PROVIDERS_AT_THIS_REVISION\s*=\s*\(([^)]*)\)", source
        )
        assert m, "the provider list must stay a literal in the revision"
        assert "oauth_a2a" in m.group(1)
        assert "entra_a2a" in m.group(1)
        # google_a2a did not exist before this revision, so no row in the
        # field can need stamping for it. It has since gained an inbound half
        # and joined ``providers.A2A_PROVIDERS`` — and this list correctly did
        # NOT follow, because a revision that has already run is not re-run
        # for a provider added afterwards. Same reasoning as
        # ``a2a2intmodel1``'s predicate.
        assert "google_a2a" not in m.group(1)

    def test_it_is_idempotent_by_construction(self, source):
        # A fresh install STAMPS this revision while an existing one EXECUTES
        # it, so the body must be safe to re-run.
        assert "NOT EXISTS" in source

    def test_it_does_not_import_the_provider_constant(self, source):
        assert "from app.a2a.providers import" not in source

    def test_it_takes_the_lock_safety_recipe(self, source):
        # Every foreign key here locks its REFERENCED table, and all four are
        # hot: CREATE TABLE locks nothing on the new tables, but the
        # statement's foreign keys still lock the parents.
        assert "terminate_lock_holders" in source
        assert "set_lock_timeout" in source
        for parent in ("integrations", "agents", "users", "assignment_runs"):
            assert parent in source

    def test_downgrade_drops_every_table_it_created(self, source):
        # Three, since the revision that split per-card state onto its own
        # table was merged in. Children before parents: a delegation
        # references the target it was sent to.
        for table in ("_DELEGATIONS", "_TARGETS", "_SETTINGS"):
            assert f"op.drop_table({table})" in source
        assert source.index("op.drop_table(_DELEGATIONS)") < source.index(
            "op.drop_table(_TARGETS)"
        )


class TestRevisionChain:
    def test_the_graph_has_a_single_head(self):
        """The invariant a rebase breaks, and the one worth asserting.

        A rebase brings main's newer revisions into the tree without touching
        ``down_revision``, so a revision chained onto one NAMED parent keeps
        pointing at what used to be the head and the graph forks in two.
        Alembic then refuses to upgrade at all — ``Multiple head revisions
        are present``.

        Naming the expected parent would need updating on every rebase, and a
        test you must remember to update is the same class of thing as a
        pointer you must remember to move. Asserting the head count needs no
        maintenance and fails the moment the fork appears.
        """
        from alembic.script import ScriptDirectory

        from app.core.migrations import _build_alembic_config

        script = ScriptDirectory.from_config(_build_alembic_config())
        heads = list(script.get_heads())
        # The COUNT, not the name — naming it is the very maintenance burden
        # this docstring argues against.
        assert len(heads) == 1, (
            f"expected a single head, got {heads}. After a rebase, re-point "
            "this branch's earliest revision at main's new head."
        )
        # A revision that silently stopped being reachable is a failure too —
        # but REACHABILITY is the property, not "is the newest": the head
        # moves on with every migration merged after this one, so asserting
        # its name here broke main the first time that happened.
        # Walking from the single head down to base must pass through this
        # revision; it does for every migration chained on top of it and
        # stops the moment the A2A work is orphaned or forked off the line.
        lineage = {rev.revision for rev in script.walk_revisions(base="base", head=heads[0])}
        assert "a2a3outbound1" in lineage, (
            f"a2a3outbound1 is not an ancestor of head {heads[0]}: the A2A "
            "revision has been orphaned from the main migration line."
        )

    def test_it_does_not_edit_merged_history(self, source):
        # a2a1srv0001 and a2a2intmodel1 are merged history and must never be
        # edited; new work chains on top of whatever the head is.
        assert 'revision: str = "a2a3outbound1"' in source
        assert 'down_revision' in source

    def test_the_settings_row_carries_direction_and_nothing_per_card(self, source):
        """Per-card state lives on the targets table, direction on settings.

        Per-card state (card, verdict, pinned origin, circuit) lives on
        ``a2a_client_targets``; the settings row carries direction only. A
        per-card column reappearing on the settings table is a regression.
        """
        # Just the settings CREATE TABLE: from its opening call to the index
        # that follows it. Slicing on the constant name alone would catch the
        # idempotency probe at the top of the function, which names all three.
        start = source.index("op.create_table(\n            _SETTINGS,")
        settings_block = source[start : source.index("op.create_index(", start)]

        assert '"direction"' in settings_block
        for per_card in ("card_url", "usability", "last_verified_at", "pinned_origin"):
            assert per_card not in settings_block, per_card


def _run_upgrade(sync_conn) -> None:
    """Execute the revision's real ``upgrade()`` against *sync_conn*.

    Loaded by path, since ``alembic/versions`` is not a package.
    """
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location(REVISION.stem, REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(sync_conn)):
        module.upgrade()


@pytest.mark.integration
class TestItRunsOnAnExistingInstall:
    """``run_migrations`` calls ``create_all`` before ``alembic upgrade``, so
    on an existing install all three tables exist, empty, when this body
    runs. The backfill must run anyway."""

    @pytest.mark.asyncio
    async def test_a_legacy_inbound_integration_is_stamped_when_the_tables_already_exist(self):
        from sqlalchemy import select

        from app.models.a2a import DIRECTION_INBOUND, A2AIntegrationSettings
        from app.models.integration import Integration
        from app.models.user import User
        from tests.test_a2a_hooks_postgres import _temp_database

        # ``_temp_database`` has already run ``create_all`` — the tables exist.
        async with _temp_database() as sm:
            async with sm() as session:
                user = User(
                    id=uuid.uuid4(),
                    email=f"legacy-{uuid.uuid4().hex[:8]}@test.local",
                    full_name="Legacy",
                    hashed_password="x",
                )
                session.add(user)
                await session.flush()
                legacy = Integration(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    provider="oauth_a2a",
                    auth_type="none",
                    name="A partner from before outbound existed",
                )
                # Not through ``create_settings``: a legacy row has no settings row.
                session.add(legacy)
                await session.commit()
                legacy_id = legacy.id

            engine = sm.kw["bind"]
            async with engine.begin() as conn:
                await conn.run_sync(_run_upgrade)

            async with sm() as session:
                rows = (await session.execute(select(A2AIntegrationSettings))).scalars().all()
                assert [(r.integration_id, r.direction) for r in rows] == [
                    (legacy_id, DIRECTION_INBOUND)
                ]

            # Idempotent: a re-run stamps nothing twice.
            async with engine.begin() as conn:
                await conn.run_sync(_run_upgrade)
            async with sm() as session:
                rows = (await session.execute(select(A2AIntegrationSettings))).scalars().all()
                assert len(rows) == 1
