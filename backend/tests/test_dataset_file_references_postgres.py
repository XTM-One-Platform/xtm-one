"""Which files a dataset lets go of are deleted, decided on a real PostgreSQL.

The deciding half of ``_delete_dataset_files_if_unreferenced`` is SQL: the
candidate rows read ``FOR UPDATE``, the foreign keys pointing at
``file_attachments``, one pass per column that names files by id as text. So it
runs here against a real database, through the routes: every holder of a
reference keeps its file and only the reference goes with the entry, the scan
finds exactly what an ``ILIKE`` per id would once hyphens are ignored, a dataset
larger than the bind parameter limit still deletes, and the rows stay locked
until the commit.
"""

from __future__ import annotations

import itertools
import json
import random
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Text, cast, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

import app.models  # noqa: F401 - registers every mapper: the whole schema is created
from app.api.v1 import datasets as datasets_api
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.audit_log import AuditLog
from app.models.chat import Conversation, Message
from app.models.dataset import Dataset, DatasetEntry
from app.models.file_attachment import FileAttachment
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.orchestration import OrchestrationExecution
from app.models.presentation import Presentation, PresentationVersion
from app.models.runner import Runner, RunnerCommand
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.workset import WorksetItem
from app.services.workset import resolve_workset
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _database():
    return scratch_database("dsfiles", tables=ALL_TABLES, extensions=("vector",))


@pytest.fixture
def purged(monkeypatch):
    """Storage keys the routes hand to the object store once they committed."""
    keys: list[str] = []

    async def _purge(found):
        keys.extend(found)
        return len(found)

    monkeypatch.setattr("app.services.file_cleanup.purge_storage_objects", _purge)
    return keys


async def _user(session, *, admin: bool) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"ds-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Dataset Test",
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _dataset(session, owner: User) -> Dataset:
    dataset = Dataset(id=uuid.uuid4(), user_id=owner.id, name=f"Bench {uuid.uuid4().hex[:6]}")
    session.add(dataset)
    await session.flush()
    return dataset


#: Positions are unique within a dataset.
_POSITIONS = itertools.count()


async def _entry(session, dataset: Dataset, file_ids=None) -> DatasetEntry:
    entry = DatasetEntry(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        position=next(_POSITIONS),
        input_prompt="q",
        expected_answer="a",
        file_ids=file_ids,
    )
    session.add(entry)
    await session.flush()
    return entry


def _file(owner: User | None, *, minted_for: uuid.UUID | str = "import", source: str = "dataset", **bindings):
    """A stored file; a dataset upload's key names the entry it was uploaded for, like the route's."""
    file_id = uuid.uuid4()
    return FileAttachment(
        id=file_id,
        user_id=owner.id if owner else None,
        filename=f"{file_id.hex[:6]}.pdf",
        content_type="application/pdf",
        size=1,
        s3_object_name=f"dataset/{minted_for}/{file_id.hex}.pdf",
        source=source,
        **bindings,
    )


def _odd_spelling(file_id: uuid.UUID) -> str:
    """A spelling ``uuid.UUID`` reads back that is neither ``str(id)`` nor ``id.hex``."""
    digits = file_id.hex.upper()
    return f"{{{digits[:12]}-{digits[12:15]}-{digits[15:]}}}"


async def _remaining(scratch, ids) -> set[uuid.UUID]:
    async with scratch.sessions() as check:
        rows = await check.execute(select(FileAttachment.id).where(FileAttachment.id.in_(list(ids))))
        return set(rows.scalars().all())


def _sqlstate(exc: BaseException) -> str | None:
    seen = exc
    while seen is not None:
        code = getattr(seen, "sqlstate", None) or getattr(seen, "pgcode", None)
        if code:
            return code
        seen = getattr(seen, "orig", None) or seen.__cause__
    return None


async def test_deleting_an_entry_deletes_only_its_unused_uploads_and_every_holder_keeps_its_file(purged):
    async with _database() as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, admin=True)
            colleague = await _user(session, admin=True)
            member = await _user(session, admin=False)
            dataset = await _dataset(session, admin)
            entry = await _entry(session, dataset)
            elsewhere = await _entry(session, await _dataset(session, colleague))
            agent = Agent(id=uuid.uuid4(), user_id=admin.id, name="Bench agent")
            conversation = Conversation(id=uuid.uuid4(), user_id=member.id)
            template = TemplateContainer(id=uuid.uuid4(), user_id=admin.id, name="Brand")
            session.add_all([agent, conversation, template])
            await session.flush()
            message = Message(id=uuid.uuid4(), conversation_id=conversation.id, role="user", content="hi")
            finished_run = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, status="completed")
            flow = Flow(id=uuid.uuid4(), user_id=admin.id, name="Flow", graph={})
            deck = Presentation(id=uuid.uuid4(), user_id=admin.id, title="Deck")
            runner = Runner(user_id=admin.id, name="desk", platform="linux", key_prefix="run-t", key_hash="h")
            session.add_all([message, finished_run, flow, deck, runner])
            await session.flush()
            workset = await resolve_workset(session, admin, key="corpus", scope="global")

            files = {
                # Dataset uploads used by nothing else: the only ones that go,
                # whoever uploaded them - another admin's upload listed here by
                # hand included, as deleting the entry always did.
                "unused": _file(admin, minted_for=entry.id),
                "colleague_upload_to_this_entry": _file(colleague, minted_for=entry.id),
                "colleague_orphan": _file(colleague, minted_for=elsewhere.id),
                "finished_execution": _file(admin, minted_for=entry.id),
                "finished_run": _file(admin, minted_for=entry.id),
                # Bound to something else, or not a dataset upload at all.
                "member_chat_file": _file(member, source="chat", conversation_id=conversation.id),
                "message_attachment": _file(admin, minted_for=entry.id, message_id=message.id, conversation_id=conversation.id),
                "run_output": _file(admin, minted_for=entry.id, assignment_run_id=finished_run.id),
                "template_asset": _file(admin, source="template_asset", template_container_id=template.id),
                # Named somewhere else that reads it back.
                "another_entry": _file(admin, minted_for=entry.id),
                "another_entry_odd_spelling": _file(admin, minted_for=entry.id),
                "template_palette": _file(admin, minted_for=entry.id),
                "pending_task": _file(admin, minted_for=entry.id),
                "delayed_run": _file(admin, minted_for=entry.id),
                "flow_run": _file(admin, minted_for=entry.id),
                "deck": _file(admin, minted_for=entry.id),
                "deck_revision": _file(admin, minted_for=entry.id),
                # Sent into a chat as a steering message: named in its
                # attachment block, never bound to the conversation.
                "steering_message": _file(admin, minted_for=entry.id),
                "workset_item": _file(admin, minted_for=entry.id),
                "runner_result": _file(admin, minted_for=entry.id),
            }
            session.add_all(files.values())
            await session.flush()
            ids = {name: row.id for name, row in files.items()}

            elsewhere_file_ids = [str(ids["another_entry"]).upper(), _odd_spelling(ids["another_entry_odd_spelling"])]
            elsewhere.file_ids = elsewhere_file_ids
            template.theme_config = {"logo_file_id": str(ids["template_palette"])}
            session.add_all(
                [
                    OrchestrationExecution(adapter="sub_agent", state="pending", payload={"file_ids": [str(ids["pending_task"])]}),
                    OrchestrationExecution(adapter="sub_agent", state="completed", payload={"file_ids": [str(ids["finished_execution"])]}),
                    AssignmentRun(agent_id=agent.id, status="delayed", trigger_payload={"file_ids": [ids["delayed_run"].hex]}),
                    AssignmentRun(agent_id=agent.id, status="completed", trigger_payload={"file_ids": [str(ids["finished_run"])]}),
                    FlowRun(flow_id=flow.id, trigger_type="manual", trigger_payload={"files": [{"file_id": str(ids["flow_run"])}]}),
                    # Inside a longer run of hex characters, as an ILIKE would still find it.
                    PresentationVersion(
                        presentation_id=deck.id,
                        revision=1,
                        title="Deck",
                        source="agent",
                        spec={"slides": [{"blocks": [{"type": "image", "file_id": str(ids["deck_revision"]).upper()}]}]},
                    ),
                    WorksetItem(workset_id=workset.id, title="doc", file_attachment_id=ids["workset_item"]),
                    Message(
                        conversation_id=conversation.id,
                        role="user",
                        content="also read this",
                        sequence=1,
                        metadata_json=json.dumps(
                            {"attachments": [{"filename": "a.pdf", "file_id": str(ids["steering_message"])}]}
                        ),
                    ),
                    RunnerCommand(runner_id=runner.id, kind="screenshot", payload={}, status="completed", result_file_id=ids["runner_result"]),
                ]
            )
            deck.spec = {"slides": [{"blocks": [{"type": "image", "file_id": f"ab{ids['deck'].hex}cd"}]}]}
            gone = uuid.uuid4()
            entry.file_ids = [str(fid) for fid in ids.values()] + ["not-a-uuid", str(gone)]
            await session.commit()

            assert await datasets_api.delete_entry(str(entry.id), user=admin, db=session) is None

        deleted = {"unused", "colleague_upload_to_this_entry", "colleague_orphan", "finished_execution", "finished_run"}
        remaining = await _remaining(scratch, ids.values())
        assert {name for name, fid in ids.items() if fid not in remaining} == deleted
        assert sorted(purged) == sorted(files[name].s3_object_name for name in deleted)
        async with scratch.sessions() as check:
            assert await check.get(DatasetEntry, entry.id) is None
            assert (await check.get(DatasetEntry, elsewhere.id)).file_ids == elsewhere_file_ids


async def test_the_single_pass_scan_finds_exactly_what_a_hyphen_blind_ilike_per_id_finds():
    rng = random.Random(4295)  # noqa: S311 - a seeded, reproducible sample
    noise = "0123456789abcdefABCDEF--xyz_ :,{}"
    candidates = [uuid.uuid4() for _ in range(60)]

    def hyphenated_anyhow(file_id: uuid.UUID) -> str:
        cuts = sorted(rng.sample(range(1, 32), rng.randint(1, 5)))
        digits = file_id.hex
        return "-".join(digits[start:end] for start, end in zip([0, *cuts], [*cuts, 32], strict=True))

    async with _database() as scratch, scratch.sessions() as session:
        dataset = await _dataset(session, await _user(session, admin=True))
        for position in range(200):
            values = []
            for _ in range(rng.randint(0, 4)):
                file_id = rng.choice(candidates)
                spelling = rng.choice([str(file_id), file_id.hex, hyphenated_anyhow(file_id)])
                if rng.random() < 0.2:  # a near miss: one character off
                    at = rng.randrange(len(spelling))
                    spelling = spelling[:at] + ("g" if spelling[at] != "g" else "h") + spelling[at + 1 :]
                spelling = "".join(c.upper() if rng.random() < 0.3 else c for c in spelling)
                pad = lambda: "".join(rng.choice(noise) for _ in range(rng.randint(0, 3)))  # noqa: E731
                values.append(pad() + spelling + pad())
            session.add(
                DatasetEntry(
                    dataset_id=dataset.id, position=position, input_prompt="q", expected_answer="a", file_ids=values
                )
            )
        await session.flush()

        odd_only = [uuid.uuid4() for _ in range(3)]
        session.add(
            DatasetEntry(
                dataset_id=dataset.id,
                position=200,
                input_prompt="q",
                expected_answer="a",
                file_ids=[_odd_spelling(fid) for fid in odd_only],
            )
        )
        await session.flush()

        column = cast(DatasetEntry.file_ids, Text)
        never_written = [uuid.uuid4() for _ in range(5)]
        asked = candidates + odd_only + never_written
        found = await datasets_api._file_ids_named_in(session, column, (), asked)

        expected = set()
        for fid in asked:
            hit = await session.scalar(
                select(DatasetEntry.id).where(func.replace(column, "-", "").ilike(f"%{fid.hex}%")).limit(1)
            )
            if hit is not None:
                expected.add(fid)
        assert found == expected
        assert set(odd_only) <= found, "a spelling neither str(id) nor id.hex still names its file"
        assert not expected & set(never_written)


async def test_deleting_a_dataset_with_more_entries_than_bind_parameters(purged):
    """asyncpg refuses a statement of more than 32767 parameters: no statement may carry one per entry."""
    async with _database() as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, admin=True)
            dataset = await _dataset(session, admin)
            entry = await _entry(session, dataset)
            upload = _file(admin, minted_for=entry.id)
            session.add(upload)
            await session.flush()
            entry.file_ids = [str(upload.id)]
            await session.execute(
                text(
                    "INSERT INTO dataset_entries (id, dataset_id, position, input_prompt, expected_answer, created_at) "
                    "SELECT gen_random_uuid(), :dataset, :after + g, 'q', 'a', now() FROM generate_series(1, 33000) AS g"
                ),
                {"dataset": dataset.id, "after": entry.position},
            )
            await session.commit()

            assert await datasets_api.delete_dataset(str(dataset.id), user=admin, db=session) is None

        assert await _remaining(scratch, [upload.id]) == set()
        assert purged == [upload.s3_object_name]
        async with scratch.sessions() as check:
            assert await check.get(Dataset, dataset.id) is None
            left = await check.scalar(select(DatasetEntry.id).where(DatasetEntry.dataset_id == dataset.id).limit(1))
            assert left is None
            audit = await check.scalar(select(AuditLog.summary).where(AuditLog.entity_id == str(dataset.id)))
            assert audit == f"Deleted dataset '{dataset.name}'"


async def test_the_candidate_rows_stay_locked_until_the_caller_commits():
    async with _database() as scratch:
        async with scratch.sessions() as setup:
            admin = await _user(setup, admin=True)
            dataset = await _dataset(setup, admin)
            entry = await _entry(setup, dataset)
            other = await _entry(setup, dataset)
            freed, kept = _file(admin, minted_for=entry.id), _file(admin, minted_for=entry.id)
            conversation = Conversation(id=uuid.uuid4(), user_id=admin.id)
            setup.add_all([freed, kept, conversation])
            await setup.flush()
            other.file_ids = [str(kept.id)]
            workset = await resolve_workset(setup, admin, key="locks", scope="global")
            await setup.commit()

        async with scratch.sessions() as deciding, scratch.sessions() as writer:
            keys = await datasets_api._delete_dataset_files_if_unreferenced(deciding, {freed.id, kept.id})
            assert keys == [freed.s3_object_name], "decided, not committed"

            # A binding of either row - the one kept included - waits for the decision.
            for file_id in (freed.id, kept.id):
                await writer.execute(text("SET LOCAL lock_timeout = '300ms'"))
                with pytest.raises(DBAPIError) as exc:
                    await writer.execute(
                        update(FileAttachment).where(FileAttachment.id == file_id).values(conversation_id=conversation.id)
                    )
                assert _sqlstate(exc.value) == "55P03"
                await writer.rollback()

            # So does a new foreign key to one (the key check takes FOR KEY SHARE on it).
            await writer.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError) as exc:
                await writer.execute(
                    insert(WorksetItem).values(
                        id=uuid.uuid4(),
                        workset_id=workset.id,
                        title="doc",
                        file_attachment_id=kept.id,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            assert _sqlstate(exc.value) == "55P03"
            await writer.rollback()

            await deciding.commit()

            # Committed: the deleted row binds nothing, the kept one binds as before.
            result = await writer.execute(
                update(FileAttachment).where(FileAttachment.id == freed.id).values(conversation_id=conversation.id)
            )
            assert result.rowcount == 0
            result = await writer.execute(
                update(FileAttachment).where(FileAttachment.id == kept.id).values(conversation_id=conversation.id)
            )
            assert result.rowcount == 1
            await writer.commit()


async def test_removing_an_entry_file_and_deleting_a_dataset_end_to_end(purged):
    async with _database() as scratch:
        async with scratch.sessions() as session:
            admin = await _user(session, admin=True)
            dataset = await _dataset(session, admin)
            first = await _entry(session, dataset)
            second = await _entry(session, dataset)
            outside = await _entry(session, await _dataset(session, admin))
            own, shared_inside, shared_outside, removed = (_file(admin, minted_for=first.id) for _ in range(4))
            session.add_all([own, shared_inside, shared_outside, removed])
            await session.flush()
            first.file_ids = [str(own.id), str(shared_inside.id), str(shared_outside.id), str(removed.id)]
            second.file_ids = [str(shared_inside.id)]
            outside.file_ids = [str(shared_outside.id)]
            await session.commit()

            # One file removed from an entry: its reference goes and, used by nothing else, its bytes.
            assert await datasets_api.remove_entry_file(str(first.id), str(removed.id), user=admin, db=session) is None
            assert purged == [removed.s3_object_name]
            assert (await session.get(DatasetEntry, first.id)).file_ids == [
                str(own.id),
                str(shared_inside.id),
                str(shared_outside.id),
            ]
            # Removing it again - a stale tab, a retried request - succeeds as before and changes nothing.
            assert await datasets_api.remove_entry_file(str(first.id), str(removed.id), user=admin, db=session) is None
            assert purged == [removed.s3_object_name]
            assert len((await session.get(DatasetEntry, first.id)).file_ids) == 3

            # A file another entry of the SAME dataset lists survives removing it from one entry...
            assert await datasets_api.remove_entry_file(str(second.id), str(shared_inside.id), user=admin, db=session) is None
            assert await _remaining(scratch, [shared_inside.id]) == {shared_inside.id}

            # ...and the whole dataset going takes the files only its entries listed.
            assert await datasets_api.delete_dataset(str(dataset.id), user=admin, db=session) is None

        assert await _remaining(scratch, [own.id, shared_inside.id, shared_outside.id, removed.id]) == {shared_outside.id}
        assert sorted(purged) == sorted(f.s3_object_name for f in (removed, own, shared_inside))
