"""Improvement proposals (#8256) against a real PostgreSQL.

Pinned here, beyond the #1044 persona cycle (``test_persona_improvement_postgres``):
the ``c3cnc8254032`` revision moves ``persona_improvements`` rows in under
their ids and the dedup key the service computes, idempotently - a re-run walks
past the rows it copied before - and puts them back on a downgrade, recreating the table a fresh install never had,
settled rows before open ones so a downgrade never trips the old one-open-rewrite index; an agent's re-filing of the same finding files nothing; a change
a person rejected is not filed again by an agent; an agent states its
measurement; one open change per target, whoever asks, but a person's request
withdraws an agent's change nobody can see with ``CONCIERGE`` off; an agent's
change is watched for the window Settings > Agents and automations > Concierge sets, ends with no
verdict when nothing read it, is kept at its end when a live watcher did, and
is rolled back by a live regression within it; a
target edited after filing is refused at approval; an administrator does not
approve a change of a private agent; a second approval waiting on the lock
sees the first one; the scheduler picks no change whose window is still open.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

import app.core.database as database
from app.core.config import settings
from app.models.agent import Agent
from app.models.improvement_proposal import (
    STATUS_FAILED,
    STATUS_KEPT,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    STATUS_WATCHING,
    ImprovementProposal,
)
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import improvement_proposals as proposals
from app.services import persona_improvement as loop
from app.services.health.settings import HealthSettings
from app.services.improvement_proposals import lifecycle, registry
from tests.scratch_database import scratch_database
from tests.test_persona_improvement_postgres import BASE_PERSONA, REWRITE, _propose, _seed
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

_REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c3cnc8254032_move_persona_improvements.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("c3cnc8254032", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()
_TABLES = [User.__table__, Agent.__table__, ImprovementProposal.__table__]


async def _migrate(engine, step) -> None:
    def _run(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            step()

    async with engine.begin() as conn:
        await conn.run_sync(_run)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


async def _legacy_row(
    engine, agent_id: uuid.UUID, status: str, row_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    row_id, baseline = row_id or uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO persona_improvements (id, agent_id, status, evaluation_id, baseline_run_id, "
                "base_persona, proposed_persona, base_version_number, applied_version_number, baseline_score) "
                "VALUES (:id, :agent, :status, :evaluation, :baseline, 'Before.', 'After.', 1, 2, 70.0)"
            ),
            {"id": row_id, "agent": agent_id, "status": status, "evaluation": uuid.uuid4(), "baseline": baseline},
        )
    return row_id, baseline


@pytest.mark.asyncio
async def test_the_revision_moves_the_persona_rewrites_in_and_back():
    async with scratch_database("proposals_mig", tables=_TABLES) as db:
        engine = db.engine
        owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                User.__table__.insert().values(
                    id=owner_id, email="mig@test.local", full_name="Migration", hashed_password="x", role="user"
                )
            )
            await conn.execute(Agent.__table__.insert().values(id=agent_id, name="Invoices", user_id=owner_id))
        await _migrate(engine, revision._create_legacy_table)
        evaluating, baseline = await _legacy_row(engine, agent_id, "evaluating")
        kept, _ = await _legacy_row(engine, agent_id, "kept")

        await _migrate(engine, revision.upgrade)
        await _migrate(engine, revision.upgrade)  # a re-run copies nothing twice

        async with engine.connect() as conn:
            rows = {
                row.id: row
                for row in (
                    await conn.execute(
                        text(
                            "SELECT id, proposal_type, target_type, target_id, agent_id, status, dedup_key, "
                            "base_content, proposed_content, evidence_refs, applied_at, decided_at "
                            "FROM improvement_proposals"
                        )
                    )
                ).all()
            }
        assert set(rows) == {evaluating, kept}
        moved = rows[evaluating]
        assert (moved.proposal_type, moved.target_type, moved.target_id, moved.agent_id, moved.status) == (
            "persona",
            "agent",
            agent_id,
            agent_id,
            STATUS_WATCHING,
        )
        assert (moved.base_content, moved.proposed_content) == ("Before.", "After.")
        assert moved.evidence_refs == [{"kind": "evaluation_run", "id": str(baseline)}]
        # The key the service computes for the same rewrite: a later request dedups against it.
        assert moved.dedup_key == proposals.dedup_key(
            "persona", "agent", agent_id, proposals.evidence_fingerprint({}, moved.evidence_refs)
        )
        assert rows[kept].status == STATUS_KEPT

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM persona_improvements"))
            await conn.execute(text("UPDATE improvement_proposals SET status = 'kept' WHERE id = :id"), {"id": evaluating})
        await _migrate(engine, revision.downgrade)

        async with engine.connect() as conn:
            back = dict((await conn.execute(text("SELECT id, status FROM persona_improvements"))).all())
        assert back == {evaluating: "kept", kept: "kept"}


@pytest.mark.asyncio
async def test_a_downgrade_closes_a_legacy_open_rewrite_before_writing_back_the_new_one():
    """Pending at upgrade, rejected under the new code, then a new one pending:
    the old table's one-open-rewrite index must never see both open."""
    async with scratch_database("proposals_mig_open", tables=_TABLES) as db:
        engine = db.engine
        owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                User.__table__.insert().values(
                    id=owner_id, email="mig-open@test.local", full_name="Migration", hashed_password="x", role="user"
                )
            )
            await conn.execute(Agent.__table__.insert().values(id=agent_id, name="Invoices", user_id=owner_id))
        await _migrate(engine, revision._create_legacy_table)
        # The new one sorts first, the legacy one last, a batch of settled rows
        # between them: a walk in id order alone writes the new one back first.
        legacy, _ = await _legacy_row(engine, agent_id, "pending_approval", uuid.UUID(int=(1 << 128) - 1))
        await _migrate(engine, revision.upgrade)
        fresh = uuid.UUID(int=1)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO improvement_proposals (id, proposal_type, target_type, target_id, agent_id, status, "
                    "dedup_key, evaluation_id, baseline_run_id) SELECT gen_random_uuid(), 'persona', 'agent', :agent, "
                    ":agent, 'kept', md5(g::text), gen_random_uuid(), gen_random_uuid() "
                    "FROM generate_series(1, :n) g"
                ),
                {"agent": agent_id, "n": revision._BATCH},
            )
            await conn.execute(text("UPDATE improvement_proposals SET status = 'rejected' WHERE id = :id"), {"id": legacy})
            await conn.execute(
                text(
                    "INSERT INTO improvement_proposals (id, proposal_type, target_type, target_id, agent_id, status, "
                    "dedup_key, evaluation_id, baseline_run_id) VALUES (:id, 'persona', 'agent', :agent, :agent, "
                    "'pending_approval', 'k-fresh', :evaluation, :baseline)"
                ),
                {"id": fresh, "agent": agent_id, "evaluation": uuid.uuid4(), "baseline": uuid.uuid4()},
            )

        await _migrate(engine, revision.downgrade)

        async with engine.connect() as conn:
            back = dict((await conn.execute(text("SELECT id, status FROM persona_improvements"))).all())
        assert (back[legacy], back[fresh], len(back)) == ("rejected", "pending_approval", revision._BATCH + 2)


@pytest.mark.asyncio
async def test_a_downgrade_leaves_out_an_open_rewrite_an_old_replica_already_holds():
    """A rewrite an old replica filed after the copy is open there only: the new
    open one is left out rather than abort the downgrade."""
    async with scratch_database("proposals_mig_replica", tables=_TABLES) as db:
        engine = db.engine
        owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                User.__table__.insert().values(
                    id=owner_id, email="mig-replica@test.local", full_name="Migration", hashed_password="x", role="user"
                )
            )
            await conn.execute(Agent.__table__.insert().values(id=agent_id, name="Invoices", user_id=owner_id))
        await _migrate(engine, revision._create_legacy_table)
        await _migrate(engine, revision.upgrade)
        late, _ = await _legacy_row(engine, agent_id, "pending_approval")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO improvement_proposals (id, proposal_type, target_type, target_id, agent_id, status, "
                    "dedup_key, evaluation_id, baseline_run_id) VALUES (:id, 'persona', 'agent', :agent, :agent, "
                    "'watching', 'k-new', :evaluation, :baseline)"
                ),
                {"id": uuid.uuid4(), "agent": agent_id, "evaluation": uuid.uuid4(), "baseline": uuid.uuid4()},
            )

        await _migrate(engine, revision.downgrade)

        async with engine.connect() as conn:
            back = dict((await conn.execute(text("SELECT id, status FROM persona_improvements"))).all())
        assert back == {late: "pending_approval"}


@pytest.mark.asyncio
async def test_a_downgrade_of_a_fresh_install_recreates_the_legacy_table():
    """A fresh install (``create_all`` + ``stamp head``) never had ``persona_improvements``."""
    async with scratch_database("proposals_fresh", tables=_TABLES) as db:
        await _migrate(db.engine, revision.upgrade)
        await _migrate(db.engine, revision.downgrade)
        async with db.engine.connect() as conn:
            legacy = await conn.scalar(text("SELECT to_regclass('persona_improvements') IS NOT NULL"))
        assert legacy is True


@pytest.mark.asyncio
async def test_a_rerun_walks_past_a_batch_it_copied_before(monkeypatch):
    async with scratch_database("proposals_mig_rerun", tables=_TABLES) as db:
        engine = db.engine
        owner_id = uuid.uuid4()
        agents = [uuid.uuid4() for _ in range(3)]
        async with engine.begin() as conn:
            await conn.execute(
                User.__table__.insert().values(
                    id=owner_id, email="rerun@test.local", full_name="Rerun", hashed_password="x", role="user"
                )
            )
            for index, agent_id in enumerate(agents):
                await conn.execute(Agent.__table__.insert().values(id=agent_id, name=f"Agent {index}", user_id=owner_id))
        await _migrate(engine, revision._create_legacy_table)
        first = [(await _legacy_row(engine, agent_id, "kept"))[0] for agent_id in agents[:2]]
        await _migrate(engine, revision.upgrade)
        # A batch of one: the rows copied before fill the first batches, and the
        # walk goes on to the one written since.
        monkeypatch.setattr(revision, "_LEGACY_IDS", revision._LEGACY_IDS.replace(f"LIMIT {revision._BATCH}", "LIMIT 1"))
        written, _ = await _legacy_row(engine, agents[2], "kept")
        later = uuid.UUID(int=(1 << 128) - 1)  # past every row copied before
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE persona_improvements SET id = :later WHERE id = :id"), {"later": later, "id": written}
            )
        await _migrate(engine, revision.upgrade)

        async with engine.connect() as conn:
            copied = set((await conn.execute(text("SELECT id FROM improvement_proposals"))).scalars())
        assert copied == {*first, later}


def _spec(agent_id: uuid.UUID, *, fingerprint: str = "finding-1", verdict: str | None = "progression"):
    return proposals.ProposalSpec(
        proposal_type="persona",
        target_id=agent_id,
        base_content=BASE_PERSONA,
        proposed_content=REWRITE,
        summary="Quote the invoice number.",
        finding={"fingerprint": fingerprint, "pattern": "Answers miss the invoice number."},
        evidence_refs=[{"kind": "trace", "id": "trace-1"}],
        experiment={"quality": {"baseline": 70.0, "candidate": 78.0}},
        experiment_verdict=verdict,
    )


async def _file(sessionmaker, seeded, **kwargs) -> proposals.FiledProposal:
    async with sessionmaker() as db:
        author = SimpleNamespace(id=uuid.uuid4(), user_id=seeded["admin"].id)
        filed = await proposals.file_proposal(db, spec=_spec(seeded["agent"].id, **kwargs), author_agent=author)
        await db.commit()
        return filed


async def _decide(sessionmaker, proposal_id, user, *, approve: bool = True) -> ImprovementProposal:
    async with sessionmaker() as db:
        actor = await db.get(User, user.id)
        if approve:
            proposal = await proposals.approve(db, user=actor, proposal_id=proposal_id)
        else:
            proposal = await proposals.reject(db, user=actor, proposal_id=proposal_id, reason="No.")
        await db.commit()
        return proposal


async def _count(sessionmaker) -> int:
    async with sessionmaker() as db:
        return int(await db.scalar(select(func.count()).select_from(ImprovementProposal)))


@pytest.mark.asyncio
async def test_a_rerun_of_the_same_finding_files_no_duplicate():
    async with _temp_database("proposal_dedup") as sessionmaker:
        seeded = await _seed(sessionmaker)
        first = await _file(sessionmaker, seeded)
        again = await _file(sessionmaker, seeded)

        assert (first.created, again.created) == (True, False)
        assert again.proposal.id == first.proposal.id
        assert await _count(sessionmaker) == 1


@pytest.mark.asyncio
async def test_a_change_a_person_rejected_is_not_filed_again_by_an_agent():
    async with _temp_database("proposal_rejected") as sessionmaker:
        seeded = await _seed(sessionmaker)
        first = await _file(sessionmaker, seeded)
        await _decide(sessionmaker, first.proposal.id, seeded["owner"], approve=False)

        again = await _file(sessionmaker, seeded)
        assert (again.created, again.proposal.id, again.proposal.status) == (False, first.proposal.id, STATUS_REJECTED)
        # New evidence is a new key.
        fresh = await _file(sessionmaker, seeded, fingerprint="finding-2")
        assert (fresh.created, fresh.proposal.status) == (True, STATUS_PENDING_APPROVAL)


@pytest.mark.asyncio
async def test_an_agent_states_its_measurement():
    async with _temp_database("proposal_unmeasured") as sessionmaker:
        seeded = await _seed(sessionmaker)
        with pytest.raises(proposals.ProposalError) as caught:
            await _file(sessionmaker, seeded, verdict=None)
        assert caught.value.status_code == 422

        unmeasured = await _file(sessionmaker, seeded, verdict="unmeasured")
        assert unmeasured.proposal.experiment_verdict == "unmeasured"


@pytest.mark.asyncio
async def test_one_open_change_per_target(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")
    async with _temp_database("proposal_open") as sessionmaker:
        seeded = await _seed(sessionmaker)
        await _file(sessionmaker, seeded)
        with pytest.raises(proposals.ProposalError) as by_agent:
            await _file(sessionmaker, seeded, fingerprint="finding-2")
        with pytest.raises(proposals.ProposalError) as by_person:
            await _propose(sessionmaker, seeded)
        assert (by_agent.value.status_code, by_person.value.status_code) == (409, 409)
        assert await _count(sessionmaker) == 1


@pytest.mark.asyncio
async def test_a_person_s_request_withdraws_an_agent_change_nobody_can_see(monkeypatch):
    """With the agents' surface off, an agent's pending change is hidden and cannot be
    rejected: it must not block the #1044 request for good."""
    monkeypatch.setattr(settings, "enabled_features", "")
    # The capability is built in since the flag went: switch the surface off here.
    monkeypatch.setattr(lifecycle, "concierge_enabled", lambda: False)
    async with _temp_database("proposal_unseen") as sessionmaker:
        seeded = await _seed(sessionmaker)
        hidden = await _file(sessionmaker, seeded)
        async with sessionmaker() as db:
            assert await loop.open_improvement(db, seeded["agent"].id) is None

        requested = await _propose(sessionmaker, seeded)

        async with sessionmaker() as db:
            withdrawn = await db.get(ImprovementProposal, hidden.proposal.id)
            mine = await db.get(ImprovementProposal, requested)
        assert (withdrawn.status, mine.status) == (STATUS_FAILED, STATUS_PENDING_APPROVAL)
        assert withdrawn.outcome_note.startswith("Withdrawn unseen")


@pytest.mark.asyncio
async def test_a_target_edited_after_filing_is_refused_at_approval():
    async with _temp_database("proposal_stale") as sessionmaker:
        seeded = await _seed(sessionmaker)
        filed = await _file(sessionmaker, seeded)
        async with sessionmaker() as db:
            (await db.get(Agent, seeded["agent"].id)).persona = "Edited by hand."
            await db.commit()

        with pytest.raises(proposals.ProposalError) as caught:
            await _decide(sessionmaker, filed.proposal.id, seeded["owner"])
        assert caught.value.status_code == 409


async def _settle_at(sessionmaker, proposal_id, now: datetime) -> proposals.SettleResult:
    async with sessionmaker() as db:
        proposal = await db.get(ImprovementProposal, proposal_id)
        result = await proposals.settle(db, proposal, now=now)
        await db.commit()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("live_watcher", [False, True], ids=["unread", "watched-live"])
async def test_an_agent_change_is_watched_for_the_set_window(monkeypatch, live_watcher):
    async with _temp_database(f"proposal_watch_{int(live_watcher)}") as sessionmaker:
        seeded = await _seed(sessionmaker)
        async with sessionmaker() as db:
            db.add(PlatformSettings(health_settings=HealthSettings(proposal_watch_days=3).model_dump(mode="json")))
            await db.commit()
        if live_watcher:

            async def _steady(_db, _proposal):
                return proposals.LiveVerdict(regressed=False)

            monkeypatch.setitem(registry._WATCHERS, "persona", _steady)
        filed = await _file(sessionmaker, seeded)
        applied = await _decide(sessionmaker, filed.proposal.id, seeded["owner"])

        assert (applied.status, applied.candidate_run_id) == (STATUS_WATCHING, None)
        assert applied.watch_until - applied.applied_at == timedelta(days=3)

        inside = await _settle_at(sessionmaker, filed.proposal.id, applied.watch_until - timedelta(minutes=1))
        assert (inside.changed, inside.status) == (False, STATUS_WATCHING)
        over = await _settle_at(sessionmaker, filed.proposal.id, applied.watch_until + timedelta(seconds=1))
        # Nothing read the change: no verdict, never "kept without a regression".
        assert (over.changed, over.status) == (True, STATUS_KEPT if live_watcher else STATUS_FAILED)
        async with sessionmaker() as db:
            assert (await db.get(Agent, seeded["agent"].id)).persona == REWRITE
            note = (await db.get(ImprovementProposal, filed.proposal.id)).outcome_note
        assert ("unmeasured" in note) is not live_watcher


@pytest.mark.asyncio
async def test_a_live_regression_in_the_window_rolls_the_change_back(monkeypatch):
    async with _temp_database("proposal_live") as sessionmaker:
        seeded = await _seed(sessionmaker)
        filed = await _file(sessionmaker, seeded)
        await _decide(sessionmaker, filed.proposal.id, seeded["owner"])

        async def _regressed(_db, _proposal):
            return proposals.LiveVerdict(regressed=True, note="Its failure rate rose to 30 %")

        monkeypatch.setitem(registry._WATCHERS, "persona", _regressed)
        monkeypatch.setattr(database, "async_session", sessionmaker)
        assert await proposals.settle_open_proposals() == 1

        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, filed.proposal.id)
            agent = await db.get(Agent, seeded["agent"].id)
        assert (proposal.status, agent.persona) == (STATUS_ROLLED_BACK, BASE_PERSONA)
        assert proposal.outcome_note == (
            f"Its failure rate rose to 30 %: the persona was rolled back to what it was, "
            f"as v{proposal.rollback_version_number}."
        )


@pytest.mark.asyncio
async def test_an_administrator_does_not_approve_an_agent_change_of_a_private_agent():
    async with _temp_database("proposal_admin") as sessionmaker:
        seeded = await _seed(sessionmaker)
        filed = await _file(sessionmaker, seeded)
        with pytest.raises(proposals.ProposalError) as caught:
            await _decide(sessionmaker, filed.proposal.id, seeded["admin"])
        assert caught.value.status_code == 403
        async with sessionmaker() as db:
            assert (await db.get(ImprovementProposal, filed.proposal.id)).status == STATUS_PENDING_APPROVAL
            assert (await db.get(Agent, seeded["agent"].id)).persona == BASE_PERSONA


@pytest.mark.asyncio
async def test_a_second_approval_waiting_on_the_lock_sees_the_first_one():
    async with _temp_database("proposal_twice") as sessionmaker:
        seeded = await _seed(sessionmaker)
        filed = await _file(sessionmaker, seeded)
        async with sessionmaker() as late:
            # The route reads the proposal and its target before the approval locks
            # them, and holds them (the identity map keeps only what is referenced).
            held = (await late.get(ImprovementProposal, filed.proposal.id), await late.get(Agent, seeded["agent"].id))
            await _decide(sessionmaker, filed.proposal.id, seeded["owner"])
            actor = await late.get(User, seeded["owner"].id)
            with pytest.raises(proposals.ProposalError) as caught:
                await proposals.approve(late, user=actor, proposal_id=filed.proposal.id)
            await late.rollback()
        assert (caught.value.status_code, len(held)) == (409, 2)
        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, filed.proposal.id)
            agent = await db.get(Agent, seeded["agent"].id)
        assert (proposal.status, agent.persona) == (STATUS_WATCHING, REWRITE)


@pytest.mark.asyncio
async def test_the_scheduler_picks_no_change_whose_window_is_still_open(monkeypatch):
    async with _temp_database("proposal_pick") as sessionmaker:
        seeded = await _seed(sessionmaker)
        filed = await _file(sessionmaker, seeded)
        await _decide(sessionmaker, filed.proposal.id, seeded["owner"])
        picked: list[uuid.UUID] = []

        async def _spy(_db, proposal, **_kwargs):
            picked.append(proposal.id)
            return proposals.SettleResult(False, proposal.status)

        monkeypatch.setattr(lifecycle, "settle", _spy)
        monkeypatch.setattr(database, "async_session", sessionmaker)
        assert await proposals.settle_open_proposals() == 0
        assert picked == []

        async with sessionmaker() as db:
            proposal = await db.get(ImprovementProposal, filed.proposal.id)
            proposal.watch_until = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        await proposals.settle_open_proposals()
        assert picked == [filed.proposal.id]
