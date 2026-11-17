"""The built-in benchmark seed, against a real PostgreSQL.

A real database because what is under test is the sync itself: which rows a
second boot leaves alone, which entry ids survive (``evaluation_results``
cascades from them), and which rows a retirement removes. The object store is
a recording fake: the seed must upload each document once and delete an object
only once no committed row names it.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.evaluations.seed import SEED_FILE_PREFIX, seed_builtin_datasets, seed_builtin_evaluations
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services.evaluation import dataset_versioning
from app.services.evaluation.launch import check_launchable
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


class _Storage:
    def __init__(self, fail_upload: bool = False):
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.delete_attempts = 0
        self.fail_upload = fail_upload
        self.fail_delete = False

    async def upload_to(self, data: bytes, key: str, content_type: str):
        if self.fail_upload:
            raise ConnectionError("object store down")
        self.uploads.append(key)

    async def delete(self, key: str):
        self.delete_attempts += 1
        if self.fail_delete:
            raise ConnectionError("object store down")
        self.deletes.append(key)


@asynccontextmanager
async def _database(*, with_agent: bool = True) -> AsyncIterator[async_sessionmaker]:
    """A scratch database with an admin and, unless the agent seed is under test, the benchmark's agent."""
    async with scratch_database("evalseed", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            admin = User(
                id=uuid.uuid4(),
                email=f"admin-{uuid.uuid4().hex[:8]}@test.local",
                full_name="Admin",
                hashed_password="x",
                role="admin",
            )
            session.add(admin)
            await session.flush()
            if with_agent:
                session.add(
                    Agent(
                        id=uuid.uuid4(),
                        name="Document Transcriber",
                        slug="ocr-benchmark-transcriber",
                        user_id=admin.id,
                        is_builtin=True,
                        is_company_managed=True,
                    )
                )
            await session.commit()
        yield db.sessions


@pytest.fixture
def storage(monkeypatch):
    from app.services import file_storage as file_storage_module

    fake = _Storage()
    monkeypatch.setattr(file_storage_module, "file_storage", fake)
    return fake


async def _dataset(session) -> Dataset | None:
    return (await session.execute(select(Dataset).where(Dataset.slug == "ocr-benchmark"))).scalar_one_or_none()


async def _entries(session, dataset_id) -> list[DatasetEntry]:
    result = await session.execute(
        select(DatasetEntry).where(DatasetEntry.dataset_id == dataset_id).order_by(DatasetEntry.position)
    )
    return list(result.scalars().all())


async def _seeded_files(session) -> list[FileAttachment]:
    result = await session.execute(
        select(FileAttachment).where(FileAttachment.s3_object_name.startswith(f"{SEED_FILE_PREFIX}/"))
    )
    return list(result.scalars().all())


def _add_a_page_break(monkeypatch) -> None:
    """A release that changes the first document under unchanged text."""
    from app.evaluations import seed as seed_module

    original = seed_module.load_seed_files

    def with_a_page_break(kind: str) -> list[dict]:
        seeds = copy.deepcopy(original(kind))
        if kind == "datasets":
            seeds[0]["entries"][0]["document"]["blocks"].insert(2, {"type": "page_break"})
        return seeds

    monkeypatch.setattr(seed_module, "load_seed_files", with_a_page_break)


async def _add_run(session, *, status: str) -> uuid.UUID:
    """A run of the built-in evaluation that scored the dataset's first entry."""
    evaluation = (await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))).scalar_one()
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=(await session.execute(select(Agent.id))).scalar_one(),
        model_id="model",
        status=status,
        trigger_reason="manual",
        score=80.0,
    )
    session.add(run)
    await session.flush()
    entry = (await _entries(session, (await _dataset(session)).id))[0]
    session.add(EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output="# Text", score=80.0))
    await session.commit()
    return run.id


async def test_first_boot_creates_the_dataset_with_one_document_per_entry(storage):
    async with _database() as sessions:
        async with sessions() as session:
            assert await seed_builtin_datasets(session, platform_mode="copilot") == 1
        async with sessions() as session:
            dataset = await _dataset(session)
            assert dataset is not None
            assert dataset.is_company_managed and dataset.is_read_only and dataset.user_id is not None
            entries = await _entries(session, dataset.id)
            files = {str(f.id): f for f in await _seeded_files(session)}
            assert len(entries) == 6 and len(files) == 6
            for entry in entries:
                assert entry.expected_answer.startswith("# ")
                assert entry.file_ids and len(entry.file_ids) == 1
                attachment = files[entry.file_ids[0]]
                assert attachment.source == "dataset" and attachment.sha256 and attachment.size > 0
                assert attachment.s3_object_name in storage.uploads
            assert {f.content_type for f in files.values()} == {"application/pdf", "image/png"}


async def test_the_documents_are_system_files_nobody_lists_or_deletes_in_work_files(storage):
    from app.api.v1.files import can_delete_file, readable_files_filter

    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            admin = (await session.execute(select(User))).scalar_one()
            files = await _seeded_files(session)
            assert len(files) == 6 and all(f.user_id is None for f in files)
            listed = set(
                (await session.execute(select(FileAttachment.id).where(readable_files_filter(admin)))).scalars()
            )
            assert not listed & {f.id for f in files}
            assert not any(can_delete_file(f, admin, conversation_owner_id=None, run=None) for f in files)
            # A row an earlier run gave an owner is made unowned again, in place.
            files[0].user_id = admin.id
            await session.commit()
            entry_ids = [e.id for e in await _entries(session, (await _dataset(session)).id)]
        uploads = len(storage.uploads)
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            assert all(f.user_id is None for f in await _seeded_files(session))
            assert [e.id for e in await _entries(session, (await _dataset(session)).id)] == entry_ids
        assert len(storage.uploads) == uploads


async def test_a_second_boot_keeps_the_entry_ids_and_uploads_nothing(storage, monkeypatch):
    from app.evaluations import seed as seed_module

    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            dataset = await _dataset(session)
            before = [(e.id, list(e.file_ids)) for e in await _entries(session, dataset.id)]
        uploads = len(storage.uploads)
        scans: list[str | None] = []
        scan = seed_module._seeded_file_rows

        async def counted_scan(db, dataset_slug=None):
            scans.append(dataset_slug)
            return await scan(db, dataset_slug)

        monkeypatch.setattr(seed_module, "_seeded_file_rows", counted_scan)
        async with sessions() as session:
            assert await seed_builtin_datasets(session, platform_mode="copilot") == 0
        async with sessions() as session:
            dataset = await _dataset(session)
            assert [(e.id, list(e.file_ids)) for e in await _entries(session, dataset.id)] == before
            assert len(await _seeded_files(session)) == 6
        assert len(storage.uploads) == uploads and storage.deletes == []
        # An ordinary boot reads its rows through their indexes, never by scanning file_attachments.
        assert scans == []


async def test_a_changed_document_under_unchanged_text_is_a_new_test_case(storage, monkeypatch):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            await seed_builtin_evaluations(session, platform_mode="copilot")
        async with sessions() as session:
            before = {e.position: e.id for e in await _entries(session, (await _dataset(session)).id)}
            run_id = await _add_run(session, status="completed")
        _add_a_page_break(monkeypatch)
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            entries = await _entries(session, (await _dataset(session)).id)
            # One entry changed, so the set is rewritten: no score measured on the
            # old document survives on an entry that now attaches the new one.
            assert all(e.id != before[e.position] for e in entries)
            assert len(await _seeded_files(session)) == 6
            # The run keeps its score; its per-entry results went with their entries.
            assert (await session.get(EvaluationRun, run_id)).score == 80.0
            assert (
                await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
            ).first() is None
        assert len(storage.deletes) == 1


async def test_a_run_in_progress_keeps_its_entries_and_documents_until_the_next_boot(storage, monkeypatch):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            await seed_builtin_evaluations(session, platform_mode="copilot")
        async with sessions() as session:
            before = [(e.id, list(e.file_ids)) for e in await _entries(session, (await _dataset(session)).id)]
            run_id = await _add_run(session, status="running")
        _add_a_page_break(monkeypatch)
        uploads = len(storage.uploads)
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            # The run read these entries and their files when it started.
            assert [(e.id, list(e.file_ids)) for e in await _entries(session, (await _dataset(session)).id)] == before
            (await session.get(EvaluationRun, run_id)).status = "completed"
            await session.commit()
        assert len(storage.uploads) == uploads and storage.deletes == []
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            entries = await _entries(session, (await _dataset(session)).id)
            assert not {e.id for e in entries} & {entry_id for entry_id, _ in before}


async def test_an_object_store_outage_keeps_the_text_and_defers_the_evaluation(storage):
    storage.fail_upload = True
    async with _database() as sessions:
        async with sessions() as session:
            assert await seed_builtin_datasets(session, platform_mode="copilot") == 1
        async with sessions() as session:
            # No runnable benchmark over entries that have no document.
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 0
        async with sessions() as session:
            dataset = await _dataset(session)
            assert len(await _entries(session, dataset.id)) == 6
            assert await _seeded_files(session) == []
            assert (await session.execute(select(Evaluation).where(Evaluation.slug.is_not(None)))).first() is None
        storage.fail_upload = False
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 1
        async with sessions() as session:
            dataset = await _dataset(session)
            assert all(e.file_ids for e in await _entries(session, dataset.id))


async def test_an_object_store_outage_during_a_document_change_keeps_the_previous_test_cases(storage, monkeypatch):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            await seed_builtin_evaluations(session, platform_mode="copilot")
        async with sessions() as session:
            before = [(e.id, list(e.file_ids)) for e in await _entries(session, (await _dataset(session)).id)]
            run_id = await _add_run(session, status="completed")
        _add_a_page_break(monkeypatch)
        storage.fail_upload = True
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            # The evaluation already exists: it keeps running on the entries and
            # documents it had, never on entries whose document could not be stored.
            assert [(e.id, list(e.file_ids)) for e in await _entries(session, (await _dataset(session)).id)] == before
            assert (
                await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))
            ).first() is not None
        assert storage.deletes == []
        storage.fail_upload = False
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            entries = await _entries(session, (await _dataset(session)).id)
            assert not {e.id for e in entries} & {entry_id for entry_id, _ in before}
            assert all(e.file_ids for e in entries)


async def test_the_evaluation_is_created_once_and_then_left_to_the_admins(storage):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 1
        async with sessions() as session:
            evaluation = (
                await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
            ).scalar_one()
            assert evaluation.is_company_managed and evaluation.execution_mode == "llm_only"
            assert evaluation.trigger_config == {}
            agent_links = (
                (await session.execute(select(EvaluationAgent).where(EvaluationAgent.evaluation_id == evaluation.id)))
                .scalars()
                .all()
            )
            dataset_links = (
                (
                    await session.execute(
                        select(EvaluationDataset).where(EvaluationDataset.evaluation_id == evaluation.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(agent_links) == 1 and len(dataset_links) == 1
            evaluation.model_ids = ["claude-sonnet-4-6"]
            evaluation.enabled = False
            await session.commit()
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 0
        async with sessions() as session:
            evaluation = (
                await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
            ).scalar_one()
            assert evaluation.model_ids == ["claude-sonnet-4-6"] and evaluation.enabled is False


async def test_an_ownerless_built_in_evaluation_is_given_its_owner_back(storage):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            await seed_builtin_evaluations(session, platform_mode="copilot")
        async with sessions() as session:
            evaluation = (
                await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
            ).scalar_one()
            owner = evaluation.user_id
            assert owner is not None
            evaluation.user_id = None
            await session.commit()
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 0
        async with sessions() as session:
            evaluation = (
                await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
            ).scalar_one()
            assert evaluation.user_id == owner


async def test_a_mode_without_built_in_content_retires_the_benchmark(storage):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            await seed_builtin_evaluations(session, platform_mode="copilot")
        async with sessions() as session:
            keys = sorted(f.s3_object_name for f in await _seeded_files(session))
            user_dataset = Dataset(id=uuid.uuid4(), name="Mine")
            session.add(user_dataset)
            await session.commit()
        async with sessions() as session:
            assert await seed_builtin_datasets(session, platform_mode="ai_disabled") == 0
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="ai_disabled") == 0
        async with sessions() as session:
            assert await _dataset(session) is None
            assert await _seeded_files(session) == []
            assert (await session.execute(select(Evaluation).where(Evaluation.slug.is_not(None)))).first() is None
            assert (await session.execute(select(Dataset).where(Dataset.name == "Mine"))).scalar_one()
        assert sorted(storage.deletes) == keys


async def test_with_the_flag_on_a_boot_seeds_the_benchmark_exactly_where_evaluation_runs(storage, monkeypatch):
    """Every mode boots with ``ENABLED_FEATURES=EVALUATION``: the benchmark exists where evaluation is on.

    Evaluation on is what shows the Evaluations page and its admin objects, so a
    mode that seeds the benchmark can always show it. A mode where evaluation
    stays off keeps what an earlier mode seeded: its boot syncs nothing.
    """
    from app.core.config import PLATFORM_MODES, settings
    from app.services import provisioning
    from app.services.ai_state import content_mode
    from app.services.evaluation.availability import evaluation_enabled

    monkeypatch.setattr(settings, "enabled_features", "EVALUATION")
    async with _database() as sessions:
        monkeypatch.setattr(provisioning, "async_session", sessions)
        for mode in PLATFORM_MODES:
            async with sessions() as session:
                before = await _dataset(session) is not None
            monkeypatch.setattr(settings, "platform_mode", mode)
            await provisioning.seed_builtin_evaluation_content(content_mode())
            async with sessions() as session:
                seeded = await _dataset(session) is not None
                evaluation = (
                    await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
                ).scalar_one_or_none()
            expected = True if evaluation_enabled() else before
            assert seeded is (evaluation is not None) is expected, mode


async def test_a_copilot_boot_links_the_benchmark_to_its_agent_and_an_xtm_one_boot_keeps_both(storage):
    """The agent the benchmark runs ships in every mode: copilot seeds it, xtm_one never deletes it."""
    from app.agents.seed import seed_builtin_agents

    async with _database(with_agent=False) as sessions:
        async with sessions() as session:
            await seed_builtin_agents(session, platform_mode="copilot")
            await session.commit()
        async with sessions() as session:
            built_in = (await session.execute(select(Agent.slug, Agent.id).where(Agent.is_builtin.is_(True)))).all()
        # Copilot seeds the agent the benchmark runs, and no other built-in:
        # every other one keeps its modes.
        assert [slug for slug, _ in built_in] == ["ocr-benchmark-transcriber"]
        transcriber_id = built_in[0][1]

        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="copilot") == 1
        async with sessions() as session:
            evaluation = (await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))).scalar_one()
            linked = (
                await session.execute(select(EvaluationAgent.agent_id).where(EvaluationAgent.evaluation_id == evaluation.id))
            ).scalars().all()
        assert linked == [transcriber_id]

        # An xtm_one boot runs the agent seed whatever the evaluation flag: the
        # agent, and so the evaluation's link to it, stay.
        async with sessions() as session:
            await seed_builtin_agents(session, platform_mode="xtm_one")
            await session.commit()
        async with sessions() as session:
            kept = (
                await session.execute(select(Agent.id).where(Agent.slug == "ocr-benchmark-transcriber"))
            ).scalar_one()
            still_linked = (
                await session.execute(select(EvaluationAgent.agent_id).where(EvaluationAgent.evaluation_id == evaluation.id))
            ).scalars().all()
        assert kept == transcriber_id
        assert still_linked == [transcriber_id]


async def test_an_xtm_one_boot_seeds_the_benchmark_without_a_schedule(storage):
    """Evaluation is a generic AI capability (dev-docs/mode-split.md S3): XTM One seeds the
    benchmark too, and nothing of it is scheduled, so a boot starts no run."""
    async with _database() as sessions:
        async with sessions() as session:
            assert await seed_builtin_datasets(session, platform_mode="xtm_one") >= 1
        async with sessions() as session:
            assert await seed_builtin_evaluations(session, platform_mode="xtm_one") == 1
        async with sessions() as session:
            evaluation = (
                await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))
            ).scalar_one()
            assert not evaluation.trigger_config


async def test_an_unreachable_store_costs_the_retirement_one_attempt_not_one_per_object(storage):
    async with _database() as sessions:
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="copilot")
        storage.fail_delete = True
        async with sessions() as session:
            await seed_builtin_datasets(session, platform_mode="ai_disabled")
        async with sessions() as session:
            assert await _dataset(session) is None and await _seeded_files(session) == []
        # The rows are gone; the six objects stay in the bucket after one failed delete.
        assert storage.delete_attempts == 1 and storage.deletes == []


# ---------------------------------------------------------------------------
# The PROSE family and dataset versions
# ---------------------------------------------------------------------------


async def _evaluation(session) -> Evaluation:
    return (await session.execute(select(Evaluation).where(Evaluation.slug == "ocr-benchmark"))).scalar_one()


async def _boot(sessions) -> None:
    async with sessions() as session:
        await seed_builtin_datasets(session, platform_mode="copilot")
    async with sessions() as session:
        await seed_builtin_evaluations(session, platform_mode="copilot")


async def _pin(sessions) -> dict[str, int]:
    """What a run started now pins, committed as a trigger commits it."""
    async with sessions() as session:
        pinned = await dataset_versioning.pin_evaluation_versions(session, (await _evaluation(session)).id)
        await session.commit()
        return pinned


async def test_a_new_install_runs_the_benchmark_on_prose(storage):
    """Both rows carry PROSE, so the benchmark passes the checks of every launch."""
    async with _database() as sessions:
        await _boot(sessions)
        async with sessions() as session:
            evaluation = await _evaluation(session)
            assert (await _dataset(session)).family == "PROSE"
            assert evaluation.family == "PROSE"
            await check_launchable(session, evaluation)


async def test_an_existing_install_gets_prose_and_keeps_an_administrators_family(storage):
    """The dataset follows its seed; the evaluation's family is filled only when missing."""
    async with _database() as sessions:
        await _boot(sessions)
        async with sessions() as session:
            # As an older install left them.
            (await _dataset(session)).family = None
            (await _evaluation(session)).family = None
            await session.commit()
        await _boot(sessions)
        async with sessions() as session:
            assert (await _dataset(session)).family == "PROSE"
            evaluation = await _evaluation(session)
            assert evaluation.family == "PROSE"
            evaluation.family = "STRUCTURED_JSON"
            await session.commit()
        await _boot(sessions)
        async with sessions() as session:
            assert (await _evaluation(session)).family == "STRUCTURED_JSON"


async def test_a_dataset_no_run_has_pinned_is_versioned_by_its_first_run(storage):
    async with _database() as sessions:
        await _boot(sessions)
        await _boot(sessions)
        async with sessions() as session:
            assert (await _dataset(session)).current_version == 0
        assert list((await _pin(sessions)).values()) == [1]


async def test_a_start_that_changes_nothing_cuts_no_version(storage):
    async with _database() as sessions:
        await _boot(sessions)
        await _pin(sessions)
        await _boot(sessions)
        async with sessions() as session:
            assert (await _dataset(session)).current_version == 1


async def test_a_family_given_after_a_run_cuts_no_version(storage):
    """A run takes its family from the evaluation, not its pinned version: the
    dataset's family lands without a version."""
    async with _database() as sessions:
        await _boot(sessions)
        async with sessions() as session:
            (await _dataset(session)).family = None
            await session.commit()
        await _pin(sessions)
        await _boot(sessions)
        async with sessions() as session:
            dataset = await _dataset(session)
            assert (dataset.family, dataset.current_version) == ("PROSE", 1)


async def test_after_a_run_a_changed_document_is_scored_by_the_next_run(storage, monkeypatch):
    """The changed document cuts a version: the next run pins it and reads the new file."""
    async with _database() as sessions:
        await _boot(sessions)
        first = await _pin(sessions)
        _add_a_page_break(monkeypatch)
        await _boot(sessions)
        second = await _pin(sessions)
        assert list(first.values()) == [1] and list(second.values()) == [2]
        async with sessions() as session:
            evaluation = await _evaluation(session)
            frozen = await dataset_versioning.load_pinned_entries(session, evaluation.id, second)
            files = {str(row.id) for row in await _seeded_files(session)}
            # Every entry of the version the next run scores names a document that exists.
            assert len(frozen) == 6
            assert all(entry.file_ids and set(entry.file_ids) <= files for entry, *_ in frozen)
            live = {e.position: e.file_ids for e in await _entries(session, (await _dataset(session)).id)}
            assert {entry.position: entry.file_ids for entry, *_ in frozen} == live


def _describe_the_first_entry_otherwise(monkeypatch) -> None:
    """A release that changes only an entry's description: same text, same document."""
    from app.evaluations import seed as seed_module

    original = seed_module.load_seed_files

    def described_otherwise(kind: str) -> list[dict]:
        seeds = copy.deepcopy(original(kind))
        if kind == "datasets":
            seeds[0]["entries"][0]["description"] = "Described otherwise"
        return seeds

    monkeypatch.setattr(seed_module, "load_seed_files", described_otherwise)


async def test_a_rewrite_that_keeps_every_file_still_cuts_a_version(storage, monkeypatch):
    """The entries are recreated with the same files: the version must point at the new ones."""
    async with _database() as sessions:
        await _boot(sessions)
        await _pin(sessions)
        _describe_the_first_entry_otherwise(monkeypatch)
        await _boot(sessions)
        second = await _pin(sessions)
        assert list(second.values()) == [2]
        async with sessions() as session:
            evaluation = await _evaluation(session)
            frozen = await dataset_versioning.load_pinned_entries(session, evaluation.id, second)
            live = await _entries(session, (await _dataset(session)).id)
            # Every result of the next run is linked to a live entry.
            assert sorted(str(entry.entry_id) for entry, *_ in frozen) == sorted(str(e.id) for e in live)
            assert "Described otherwise" in {entry.description for entry, *_ in frozen}


async def test_a_pending_run_keeps_its_version_until_it_has_run(storage, monkeypatch):
    """A run is pinned from its creation, held pending maybe for long: the sync waits for it."""
    async with _database() as sessions:
        await _boot(sessions)
        await _pin(sessions)
        async with sessions() as session:
            before = {e.position: e.id for e in await _entries(session, (await _dataset(session)).id)}
            await _add_run(session, status="pending")
        _add_a_page_break(monkeypatch)
        await _boot(sessions)
        async with sessions() as session:
            dataset = await _dataset(session)
            assert {e.position: e.id for e in await _entries(session, dataset.id)} == before
            assert dataset.current_version == 1
        assert storage.deletes == []


async def test_a_family_lands_when_the_documents_cannot(storage, monkeypatch):
    """A failed document sync still lands the family, which the launch checks."""
    from app.evaluations import seed as seed_module

    async with _database() as sessions:
        await _boot(sessions)
        async with sessions() as session:
            (await _dataset(session)).family = None
            await session.commit()
        await _pin(sessions)

        async def _store_down(*_args, **_kwargs):
            raise ConnectionError("object store down")

        monkeypatch.setattr(seed_module, "_sync_dataset_documents", _store_down)
        await _boot(sessions)
        async with sessions() as session:
            dataset = await _dataset(session)
            assert (dataset.family, dataset.current_version) == ("PROSE", 1)


async def test_two_writers_cutting_at_once_cut_one_version(storage):
    """The comparison and the cut run under the dataset's row lock: no duplicate version number."""
    import asyncio

    async with _database() as sessions:
        await _boot(sessions)
        await _pin(sessions)
        # An entry edited since the pinned version: both writers find it changed.
        async with sessions() as session:
            (await _entries(session, (await _dataset(session)).id))[0].description = "edited"
            await session.commit()

        async def _cut() -> None:
            async with sessions() as session:
                dataset = await _dataset(session)
                await dataset_versioning.cut_version_if_changed(session, dataset)
                await session.commit()

        await asyncio.gather(_cut(), _cut())
        async with sessions() as session:
            assert (await _dataset(session)).current_version == 2

