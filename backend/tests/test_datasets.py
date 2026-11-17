"""Dataset handlers: family, ground-truth material and export/import.

Calls the real ``app.api.v1.datasets`` handlers directly (the behaviour lives in
the handler bodies, not in routing) against a throwaway PostgreSQL database per
test. Skips when no PostgreSQL is reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
import zipfile

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import datasets as datasets_api
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.dataset import (
    CreateDatasetRequest,
    DatasetEntryRequest,
    UpdateDatasetRequest,
)
from app.api.v1.schemas.evaluation import CreateEvaluationRequest
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationRun
from app.models.flow import Flow
from app.models.group import Group
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# ── Dataset family and ground-truth material ──────────────────────────────────────────
# Link compatibility (409 on conflict) and ``family_conflicts`` on PATCH,
# ``needs_family``, ``label_vocabulary`` only on a ``CLASSIFICATION`` dataset, the
# ``output_schema`` family gate, ``GET /datasets?family=...``, the read-only unlock,
# entry-level vocabulary validation and the ``seed-ground-truth`` preview.


_TABLES = [
    User.__table__,
    Group.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationDataset.__table__,
]
# ``file_attachments`` is absent on purpose: it has foreign keys to half the schema.
# The one test that stores a file builds the full schema (``_make_db_full``).


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("ds", tables=_TABLES))
    return db.sessions, stack.aclose


@pytest.fixture(autouse=True)
def _stub_audit_and_groups(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(datasets_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(datasets_api, "log_audit", _no_audit)
    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(evaluations_api, "log_audit", _no_audit)


async def _admin(session) -> User:
    admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    await session.flush()
    await session.commit()
    return admin


async def _create_dataset(session, admin, **kwargs) -> dict:
    body = CreateDatasetRequest(name=kwargs.pop("name", "ds"), **kwargs)
    return await datasets_api.create_dataset(
        body=body, user=admin, db=session, _agentic_gate=None
    )


async def _create_evaluation(session, *, family=None) -> Evaluation:
    evaluation = Evaluation(name="Eval", family=family)
    session.add(evaluation)
    await session.flush()
    await session.commit()
    return evaluation


# --- link compatibility ---


async def test_link_rejects_conflicting_families():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s, family="STIX_BUNDLE")
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            with pytest.raises(HTTPException) as exc:
                await datasets_api.link_dataset_to_evaluation(
                    dataset_id=created["id"],
                    evaluation_id=str(evaluation.id),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 409
            assert "STIX_BUNDLE" in exc.value.detail
            assert "CLASSIFICATION" in exc.value.detail
    finally:
        await cleanup()


@pytest.mark.parametrize(
    "dataset_family,evaluation_family",
    [
        ("STIX_BUNDLE", "STIX_BUNDLE"),
        (None, "STIX_BUNDLE"),
        ("STIX_BUNDLE", None),
        (None, None),
    ],
    ids=["same-family", "dataset-family-less", "evaluation-family-less", "both-family-less"],
)
async def test_link_succeeds_when_families_match_or_either_side_has_none(
    dataset_family, evaluation_family
):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s, family=evaluation_family)
            created = await _create_dataset(s, admin, family=dataset_family)
            result = await datasets_api.link_dataset_to_evaluation(
                dataset_id=created["id"],
                evaluation_id=str(evaluation.id),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert result["status"] == "linked"
    finally:
        await cleanup()


# Creating an evaluation with ``dataset_ids`` links them under the same rule:
# the creation page makes a dataset with the family chosen at that moment, which a
# later family change would otherwise leave linked under another one.


async def _create_evaluation_linking(session, admin, *, family, dataset_ids) -> dict:
    body = CreateEvaluationRequest(
        name="Eval", family=family, scorer_selection=[], dataset_ids=dataset_ids
    )
    return await evaluations_api.create_evaluation(
        body=body, user=admin, db=session, _agentic_gate=None
    )


async def test_create_evaluation_refuses_a_dataset_of_another_family():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            matching = await _create_dataset(s, admin, name="Bundles", family="STIX_BUNDLE")
            other = await _create_dataset(s, admin, name="Invoices", family="STRUCTURED_JSON")
            with pytest.raises(HTTPException) as exc:
                await _create_evaluation_linking(
                    s, admin, family="STIX_BUNDLE", dataset_ids=[matching["id"], other["id"]]
                )
            assert exc.value.status_code == 409
            assert "Invoices" in exc.value.detail
            assert "STRUCTURED_JSON" in exc.value.detail
            assert "Bundles" not in exc.value.detail
    finally:
        await cleanup()


@pytest.mark.parametrize("dataset_family", ["STIX_BUNDLE", None], ids=["same-family", "family-less"])
async def test_create_evaluation_links_a_dataset_of_its_family_or_none(dataset_family):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family=dataset_family)
            resp = await _create_evaluation_linking(
                s, admin, family="STIX_BUNDLE", dataset_ids=[created["id"]]
            )
            linked = (
                await s.execute(
                    select(EvaluationDataset.dataset_id).where(
                        EvaluationDataset.evaluation_id == uuid.UUID(resp["id"])
                    )
                )
            ).scalars().all()
            assert [str(dataset_id) for dataset_id in linked] == [created["id"]]
    finally:
        await cleanup()


async def test_patch_dataset_family_surfaces_conflicts_without_unlinking():
    """A family change that conflicts with a linked evaluation reports the conflict
    and leaves the link in place: report, never silently fix."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s, family="STIX_BUNDLE")
            created = await _create_dataset(s, admin, family=None)
            await datasets_api.link_dataset_to_evaluation(
                dataset_id=created["id"],
                evaluation_id=str(evaluation.id),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family="CLASSIFICATION"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] == "CLASSIFICATION"
            assert resp["family_conflicts"]
            assert resp["family_conflicts"][0].evaluation_id == str(evaluation.id)

            # The link itself must survive the conflict report.
            link_result = await s.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.evaluation_id == evaluation.id,
                    EvaluationDataset.dataset_id == uuid.UUID(created["id"]),
                )
            )
            assert link_result.scalar_one_or_none() is not None
    finally:
        await cleanup()


async def test_patch_evaluation_family_surfaces_conflicts_the_same_way(monkeypatch):
    """The same conflict report from the evaluation side."""
    from app.api.v1 import evaluations as evaluations_api
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    async def _no_groups(_db, _uid):
        return []

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(evaluations_api, "log_audit", _no_audit)

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s, family=None)
            created = await _create_dataset(s, admin, family="STIX_BUNDLE")
            await datasets_api.link_dataset_to_evaluation(
                dataset_id=created["id"],
                evaluation_id=str(evaluation.id),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            resp = await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation.id),
                body=UpdateEvaluationRequest(family="CLASSIFICATION"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] == "CLASSIFICATION"
            assert resp["family_conflicts"]
            assert resp["family_conflicts"][0].dataset_id == created["id"]
    finally:
        await cleanup()


# --- appending takes the next position ---
#
# ``max(position) or -1`` read a maximum of 0 as an empty list, so every item
# appended after the first one took position 0.


async def test_entries_added_one_at_a_time_take_the_next_position():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin)
            for prompt in ("first", "second", "third"):
                await datasets_api.add_entry(
                    dataset_id=created["id"],
                    body=DatasetEntryRequest(input_prompt=prompt, expected_answer="a"),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            rows = (
                await s.execute(
                    select(DatasetEntry.input_prompt, DatasetEntry.position)
                    .where(DatasetEntry.dataset_id == uuid.UUID(created["id"]))
                    .order_by(DatasetEntry.created_at)
                )
            ).all()
        assert [tuple(row) for row in rows] == [("first", 0), ("second", 1), ("third", 2)]
    finally:
        await cleanup()


async def test_datasets_linked_one_at_a_time_take_the_next_position():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s)
            names = ("first", "second", "third")
            for name in names:
                created = await _create_dataset(s, admin, name=name)
                await datasets_api.link_dataset_to_evaluation(
                    dataset_id=created["id"],
                    evaluation_id=str(evaluation.id),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            rows = (
                await s.execute(
                    select(Dataset.name, EvaluationDataset.position)
                    .join(Dataset, Dataset.id == EvaluationDataset.dataset_id)
                    .where(EvaluationDataset.evaluation_id == evaluation.id)
                    .order_by(EvaluationDataset.created_at)
                )
            ).all()
        assert [tuple(row) for row in rows] == [("first", 0), ("second", 1), ("third", 2)]
    finally:
        await cleanup()


@pytest.mark.parametrize("append", ["entry", "link"])
async def test_an_append_waits_for_the_one_in_progress_under_the_same_parent(append):
    """Two appends at once would read the same ``max(position)``: the second waits on the
    parent's row until the first commits, then takes the next position."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s, sm() as in_progress:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s)
            first = uuid.UUID((await _create_dataset(s, admin, name="first"))["id"])
            second = (await _create_dataset(s, admin, name="second"))["id"]
            await s.commit()

            if append == "entry":
                await datasets_api._lock_for_append(in_progress, Dataset, first)
                in_progress.add(DatasetEntry(dataset_id=first, position=0, expected_answer="a"))
                appending = datasets_api.add_entry(
                    dataset_id=str(first),
                    body=DatasetEntryRequest(input_prompt="next", expected_answer="a"),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
                positions = select(DatasetEntry.position).where(DatasetEntry.dataset_id == first)
            else:
                await datasets_api._lock_for_append(in_progress, Evaluation, evaluation.id)
                in_progress.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=first, position=0))
                appending = datasets_api.link_dataset_to_evaluation(
                    dataset_id=second, evaluation_id=str(evaluation.id), user=admin, db=s, _ai_gate=None
                )
                positions = select(EvaluationDataset.position).where(
                    EvaluationDataset.evaluation_id == evaluation.id
                )
            await in_progress.flush()

            task = asyncio.create_task(appending)
            await asyncio.wait({task}, timeout=0.5)
            assert not task.done(), "the append did not wait for the one in progress"
            await in_progress.commit()
            await task

            assert sorted((await s.execute(positions)).scalars()) == [0, 1]
    finally:
        await cleanup()


# --- needs_family ---


async def test_needs_family_flag_on_list_and_detail():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            no_family = await _create_dataset(s, admin, name="no-family", family=None)
            with_family = await _create_dataset(
                s, admin, name="with-family", family="PROSE"
            )

            listing = await datasets_api.list_datasets(
                search=None, family=None, user=admin, db=s
            )
            by_id = {d["id"]: d for d in listing}
            assert by_id[no_family["id"]]["needs_family"] is True
            assert by_id[with_family["id"]]["needs_family"] is False

            detail = await datasets_api.get_dataset(
                dataset_id=no_family["id"], user=admin, db=s
            )
            assert detail["needs_family"] is True
    finally:
        await cleanup()



async def test_linked_datasets_say_which_blocks_a_launch_and_which_conflicts():
    """The evaluation's Dataset tab reads each linked dataset's family, whether a
    missing one blocks a launch, and a conflict a later family change left."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = await _create_evaluation(s, family="STIX_BUNDLE")
            same = await _create_dataset(s, admin, name="same", family="STIX_BUNDLE")
            missing = await _create_dataset(s, admin, name="missing", family=None)
            changed = await _create_dataset(s, admin, name="changed", family=None)
            for dataset in (same, missing, changed):
                await datasets_api.link_dataset_to_evaluation(
                    dataset_id=dataset["id"],
                    evaluation_id=str(evaluation.id),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            await datasets_api.update_dataset(
                dataset_id=changed["id"],
                body=UpdateDatasetRequest(family="CLASSIFICATION"),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            linked = await datasets_api.list_evaluation_datasets(
                evaluation_id=str(evaluation.id), user=admin, db=s
            )
            by_name = {row["dataset_name"]: row for row in linked}
            assert {
                name: (row["family"], row["needs_family"], row["family_conflict"])
                for name, row in by_name.items()
            } == {
                "same": ("STIX_BUNDLE", False, None),
                "missing": (None, True, None),
                "changed": (
                    "CLASSIFICATION",
                    False,
                    "Dataset family 'CLASSIFICATION' conflicts with evaluation "
                    "family 'STIX_BUNDLE'.",
                ),
            }
    finally:
        await cleanup()


# --- label vocabulary ---


@pytest.mark.parametrize("family", ["STIX_BUNDLE", "STRUCTURED_JSON", "PROSE", None])
async def test_label_vocabulary_rejected_for_non_classification_family(family):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family=family)
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(label_vocabulary=["a", "b"]),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_label_vocabulary_accepted_for_classification_family():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=["spam", "ham"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["label_vocabulary"] == ["spam", "ham"]
    finally:
        await cleanup()


async def test_create_dataset_reports_vocabulary_family_error_before_entry_label_mismatch():
    """A ``label_vocabulary`` on a non-CLASSIFICATION dataset with inline entries
    reports "can only be set on a CLASSIFICATION dataset", not a per-entry label
    mismatch against the unvalidated vocabulary."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _create_dataset(
                    s,
                    admin,
                    family="PROSE",
                    label_vocabulary=["spam", "ham"],
                    entries=[
                        DatasetEntryRequest(expected_answer="something else entirely")
                    ],
                )
            assert exc.value.status_code == 422
            assert "CLASSIFICATION dataset" in exc.value.detail
    finally:
        await cleanup()


async def test_patch_family_omitted_leaves_it_unchanged():
    """Omitting ``family`` from the PATCH body leaves the dataset's family untouched.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STIX_BUNDLE")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(name="renamed"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["name"] == "renamed"
            assert resp["family"] == "STIX_BUNDLE"
    finally:
        await cleanup()


async def test_patch_family_explicit_null_clears_it():
    """An explicit ``null`` on ``family`` clears it, unlike an omitted field."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STIX_BUNDLE")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family=None),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] is None
            assert resp["needs_family"] is True
    finally:
        await cleanup()


async def test_patch_label_vocabulary_explicit_null_clears_it():
    """An explicit ``null`` on ``label_vocabulary`` clears it; omitted means no change."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=["spam", "ham"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=None),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["label_vocabulary"] is None
    finally:
        await cleanup()


async def test_label_vocabulary_is_stored_as_typed_even_when_labels_score_as_one():
    """``Phishing`` and ``phishing.`` are one label when scored: a vocabulary that
    declares both is kept as typed, so an older export keeps importing."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=["Phishing", "phishing.", "C#", "C"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["label_vocabulary"] == ["Phishing", "phishing.", "C#", "C"]
    finally:
        await cleanup()


async def test_label_vocabulary_refuses_a_label_made_only_of_marks():
    """``?`` normalizes to nothing, so it could never match an answer."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(label_vocabulary=["spam", "?"]),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_patch_label_vocabulary_empty_list_rejected_not_treated_as_clear():
    """An explicit empty ``label_vocabulary: []`` is a 422, not an alias of ``null``:
    a client could otherwise believe it declared a vocabulary it cleared."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=["spam", "ham"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(label_vocabulary=[]),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


_JSON_SCHEMA = {"type": "json_schema", "definition": {"type": "object"}}


@pytest.mark.parametrize(
    ("created", "update", "snapshot"),
    [
        pytest.param(
            {"family": "CLASSIFICATION", "label_vocabulary": ["spam", "ham"]},
            {"family": "PROSE"},
            {"label_vocabulary": None, "output_schema": None},
            id="family-clearing-the-vocabulary",
        ),
        pytest.param(
            {"family": "CLASSIFICATION", "label_vocabulary": ["spam", "ham"]},
            {"label_vocabulary": ["spam", "ham", "eggs"]},
            {"label_vocabulary": ["spam", "ham", "eggs"], "output_schema": None},
            id="vocabulary",
        ),
        pytest.param(
            {"family": "STRUCTURED_JSON", "answer": '{"label": "spam"}'},
            {"output_schema": _JSON_SCHEMA},
            {"label_vocabulary": None, "output_schema": _JSON_SCHEMA},
            id="answer-schema",
        ),
    ],
)
async def test_a_dataset_shape_change_is_a_new_version_the_next_run_pins(created, update, snapshot):
    """Runs read the vocabulary and schema from their pinned version.

    Changing one of them alone must create a version holding it, or the next run pins the
    previous shape.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = dict(created)
            answer = created.pop("answer", '["spam"]')
            dataset = await _create_dataset(
                s, admin, entries=[DatasetEntryRequest(input_prompt="p", expected_answer=answer)], **created
            )
            await datasets_api.update_dataset(
                dataset_id=dataset["id"],
                body=UpdateDatasetRequest(**update),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            latest = (
                await s.execute(
                    select(DatasetVersion)
                    .where(DatasetVersion.dataset_id == uuid.UUID(dataset["id"]))
                    .order_by(DatasetVersion.version_number.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert latest.version_number == 2
            assert {
                "label_vocabulary": latest.label_vocabulary,
                "output_schema": latest.output_schema,
            } == snapshot
            assert latest.entry_count == 1
    finally:
        await cleanup()


async def test_a_metadata_only_update_creates_no_version():
    """A rename changes nothing a run reads: the pinned version stays current."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            dataset = await _create_dataset(
                s,
                admin,
                family="PROSE",
                entries=[DatasetEntryRequest(input_prompt="p", expected_answer="a")],
            )
            resp = await datasets_api.update_dataset(
                dataset_id=dataset["id"],
                body=UpdateDatasetRequest(name="renamed", family="PROSE"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["current_version"] == 1
    finally:
        await cleanup()


async def test_a_reordered_vocabulary_creates_no_version():
    """Scoring reads the vocabulary as a set: a reorder changes nothing a run reads."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            dataset = await _create_dataset(
                s,
                admin,
                family="CLASSIFICATION",
                label_vocabulary=["spam", "ham"],
                entries=[DatasetEntryRequest(input_prompt="p", expected_answer='["spam"]')],
            )
            resp = await datasets_api.update_dataset(
                dataset_id=dataset["id"],
                body=UpdateDatasetRequest(label_vocabulary=["ham", "spam"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["label_vocabulary"] == ["ham", "spam"]
            assert resp["current_version"] == 1
    finally:
        await cleanup()


async def test_patch_family_away_from_classification_auto_clears_stale_vocabulary():
    """Switching ``family`` away from CLASSIFICATION clears the vocabulary, which only
    that family may carry."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="CLASSIFICATION")
            await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(label_vocabulary=["spam", "ham"]),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family="PROSE"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] == "PROSE"
            assert resp["label_vocabulary"] is None
    finally:
        await cleanup()


async def test_unrelated_update_does_not_clear_legacy_vocabulary_when_family_untouched():
    """The vocabulary auto-clear fires only when this request changes ``family``.

    A family-less row that already carries a vocabulary keeps it through an unrelated
    PATCH (here, a rename).
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            # Built directly, past the API gate: family unset but a vocabulary present.
            ds = Dataset(
                name="legacy",
                user_id=admin.id,
                family=None,
                label_vocabulary=["spam", "ham"],
            )
            s.add(ds)
            await s.commit()
            await s.refresh(ds)

            resp = await datasets_api.update_dataset(
                dataset_id=str(ds.id),
                body=UpdateDatasetRequest(name="renamed"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["name"] == "renamed"
            assert resp["label_vocabulary"] == ["spam", "ham"]
    finally:
        await cleanup()


# --- output_schema family gate ---


async def test_create_dataset_rejects_output_schema_on_non_structured_json():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            body = CreateDatasetRequest(
                name="ds",
                family="PROSE",
                output_schema={"type": "json_schema", "definition": {"type": "object"}},
            )
            with pytest.raises(HTTPException) as exc:
                await datasets_api.create_dataset(
                    body=body, user=admin, db=s, _agentic_gate=None
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_create_dataset_accepts_output_schema_on_structured_json():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s,
                admin,
                family="STRUCTURED_JSON",
                output_schema={"type": "json_schema", "definition": {"type": "object"}},
            )
            assert created["output_schema"]["type"] == "json_schema"
    finally:
        await cleanup()


@pytest.mark.parametrize("family", ["STIX_BUNDLE", "CLASSIFICATION", "PROSE", None])
async def test_patch_output_schema_rejected_for_non_structured_json_family(family):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family=family)
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(
                        output_schema={
                            "type": "json_schema",
                            "definition": {"type": "object"},
                        }
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_patch_output_schema_accepted_for_structured_json_family():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(
                    output_schema={"type": "json_schema", "definition": {"type": "object"}}
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["output_schema"]["type"] == "json_schema"
    finally:
        await cleanup()


async def test_patch_family_and_output_schema_together_validates_against_new_family():
    """A PATCH changing both ``family`` and ``output_schema`` validates the schema
    against the new family, as for ``label_vocabulary``."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="PROSE")
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(
                    family="STRUCTURED_JSON",
                    output_schema={"type": "json_schema", "definition": {"type": "object"}},
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] == "STRUCTURED_JSON"
            assert resp["output_schema"]["type"] == "json_schema"
    finally:
        await cleanup()


async def test_patch_family_away_from_structured_json_auto_clears_stale_output_schema():
    """Switching ``family`` away from STRUCTURED_JSON clears the schema, which only that
    family may carry."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s,
                admin,
                family="STRUCTURED_JSON",
                output_schema={"type": "json_schema", "definition": {"type": "object"}},
            )
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family="PROSE"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["family"] == "PROSE"
            assert resp["output_schema"] is None
    finally:
        await cleanup()


async def test_unrelated_update_does_not_clear_legacy_output_schema_when_family_untouched():
    """A family-less row keeps its ``output_schema`` through an unrelated PATCH that
    never touches ``family``."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            ds = Dataset(
                name="legacy",
                user_id=admin.id,
                family=None,
                output_schema={"type": "json_schema", "definition": {"type": "object"}},
            )
            s.add(ds)
            await s.commit()
            await s.refresh(ds)

            resp = await datasets_api.update_dataset(
                dataset_id=str(ds.id),
                body=UpdateDatasetRequest(name="renamed"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["name"] == "renamed"
            assert resp["output_schema"]["type"] == "json_schema"
    finally:
        await cleanup()


async def test_validate_output_schema_rejects_retired_regex_type():
    """Only ``json_schema`` is accepted, even on a STRUCTURED_JSON dataset: ``regex``
    and ``tool_sequence`` are refused at write time."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON")
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(
                        output_schema={"type": "regex", "definition": "^ok$"}
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "json_schema" in exc.value.detail
    finally:
        await cleanup()


async def test_add_entry_output_schema_rejected_for_non_structured_json_dataset():
    """An entry-level ``output_schema`` is refused on a non-STRUCTURED_JSON dataset too."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="PROSE")
            with pytest.raises(HTTPException) as exc:
                await datasets_api.add_entry(
                    dataset_id=created["id"],
                    body=DatasetEntryRequest(
                        expected_answer="{}",
                        output_schema={
                            "type": "json_schema",
                            "definition": {"type": "object"},
                        },
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_add_entry_output_schema_accepted_for_structured_json_dataset():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON")
            resp = await datasets_api.add_entry(
                dataset_id=created["id"],
                body=DatasetEntryRequest(
                    expected_answer="{}",
                    output_schema={"type": "json_schema", "definition": {"type": "object"}},
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["output_schema"]["type"] == "json_schema"
    finally:
        await cleanup()


async def test_update_entry_output_schema_rejected_for_non_structured_json_dataset():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON")
            entry_resp = await datasets_api.add_entry(
                dataset_id=created["id"],
                body=DatasetEntryRequest(
                    expected_answer="{}",
                    output_schema={"type": "json_schema", "definition": {"type": "object"}},
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            # Move the dataset off STRUCTURED_JSON, then edit the entry with a schema.
            await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family="PROSE"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_entry(
                    entry_id=entry_resp["id"],
                    body=DatasetEntryRequest(
                        expected_answer="{}",
                        output_schema={
                            "type": "json_schema",
                            "definition": {"type": "object"},
                        },
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


# --- family filter and read-only ---


async def test_list_datasets_family_filter_includes_family_less():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            matching = await _create_dataset(s, admin, name="matching", family="STIX_BUNDLE")
            other = await _create_dataset(s, admin, name="other", family="PROSE")
            family_less = await _create_dataset(s, admin, name="family-less", family=None)

            listing = await datasets_api.list_datasets(
                search=None, family="STIX_BUNDLE", user=admin, db=s
            )
            ids = {d["id"] for d in listing}
            assert matching["id"] in ids
            assert family_less["id"] in ids
            assert other["id"] not in ids
    finally:
        await cleanup()


@pytest.mark.parametrize("family", ["", "NOT_A_FAMILY"], ids=["empty", "unknown"])
async def test_list_datasets_rejects_an_invalid_family_filter(family):
    """An empty or unknown ``?family=`` is a 422, never a bypass returning every
    dataset or only the family-less ones."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            await _create_dataset(s, admin, name="ds", family="STIX_BUNDLE")
            with pytest.raises(HTTPException) as exc:
                await datasets_api.list_datasets(
                    search=None, family=family, user=admin, db=s
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


async def test_unlocking_read_only_dataset_applies_bundled_field_changes():
    """A request that unlocks a read-only dataset applies the other fields in the
    same body: the settings UI always sends them together."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, name="ds", is_read_only=True)
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(
                    name="renamed", description="new desc", is_read_only=False
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert resp["is_read_only"] is False
            assert resp["name"] == "renamed"
            assert resp["description"] == "new desc"
    finally:
        await cleanup()


async def test_read_only_dataset_still_rejects_other_updates_without_unlocking():
    """Without an explicit ``is_read_only: false`` in the same request, a
    read-only dataset must still reject other field mutations."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, name="ds", is_read_only=True)
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(name="renamed"),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 403
    finally:
        await cleanup()


async def _grantees(session) -> tuple[str, str]:
    """A user and a group a dataset can be shared with."""
    member = User(email="m@x.io", full_name="Member", hashed_password="x", role="user")
    group = Group(name="Analysts")
    session.add_all([member, group])
    await session.flush()
    await session.commit()
    return str(member.id), str(group.id)


# Each sharing field on its own: the dataset it starts from, the update, and what the
# response then holds.
_SHARING_CHANGES = {
    "shared_user_ids": (
        lambda user, group: {},
        lambda user, group: {"shared_user_ids": [user]},
        lambda user, group: {"shared_user_ids": [user]},
    ),
    "shared_user_permissions": (
        lambda user, group: {"shared_user_ids": [user]},
        lambda user, group: {"shared_user_permissions": {user: "config"}},
        lambda user, group: {"shared_user_permissions": {user: "config"}},
    ),
    "shared_group_ids": (
        lambda user, group: {},
        lambda user, group: {"shared_group_ids": [group]},
        lambda user, group: {"shared_group_ids": [group]},
    ),
    "shared_group_permissions": (
        lambda user, group: {"shared_group_ids": [group]},
        lambda user, group: {"shared_group_permissions": {group: "config"}},
        lambda user, group: {"shared_group_permissions": {group: "config"}},
    ),
    "is_company_managed": (
        lambda user, group: {},
        lambda user, group: {"is_company_managed": True},
        lambda user, group: {"is_company_managed": True},
    ),
    "is_company_managed_off": (
        lambda user, group: {"is_company_managed": True},
        lambda user, group: {"is_company_managed": False},
        lambda user, group: {"is_company_managed": False},
    ),
    "access_restricted": (
        lambda user, group: {"is_company_managed": True},
        lambda user, group: {"access_restricted": True},
        lambda user, group: {"access_restricted": True},
    ),
}


@pytest.mark.parametrize("field", list(_SHARING_CHANGES))
async def test_a_read_only_dataset_takes_a_sharing_change_and_stays_read_only(field):
    """Read-only locks the content, not who can open the dataset: each sharing
    field changes on its own, without unlocking it."""
    start, change, expected = _SHARING_CHANGES[field]
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            user, group = await _grantees(s)
            created = await _create_dataset(s, admin, name="ds", is_read_only=True, **start(user, group))
            resp = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(**change(user, group)),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert {key: resp[key] for key in expected(user, group)} == expected(user, group)
            assert resp["is_read_only"] is True
    finally:
        await cleanup()


# Every content field, an explicit ``null`` included: sent beside a sharing field,
# each keeps the request refused.
_CONTENT_CHANGES = {
    "name": {"name": "renamed"},
    "description": {"description": "new desc"},
    "evaluation_instructions": {"evaluation_instructions": "Be strict."},
    "output_schema": {"output_schema": {}},
    "family": {"family": None},
    "label_vocabulary": {"label_vocabulary": None},
}


@pytest.mark.parametrize("field", list(_CONTENT_CHANGES))
async def test_a_read_only_dataset_refuses_a_content_change_sent_with_a_sharing_change(field):
    """A sharing field does not carry a content change through: the request is
    refused whole, and the dataset keeps its content and its sharing."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            user, _group = await _grantees(s)
            created = await _create_dataset(
                s,
                admin,
                name="ds",
                description="desc",
                evaluation_instructions="Be fair.",
                family="CLASSIFICATION",
                label_vocabulary=["spam", "ham"],
                is_read_only=True,
            )
            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(**_CONTENT_CHANGES[field], shared_user_ids=[user]),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 403
        async with sm() as s:
            stored = await s.get(Dataset, uuid.UUID(created["id"]))
            assert (
                stored.name,
                stored.description,
                stored.evaluation_instructions,
                stored.family,
                stored.label_vocabulary,
                stored.shared_user_ids,
                stored.is_read_only,
            ) == ("ds", "desc", "Be fair.", "CLASSIFICATION", ["spam", "ham"], None, True)
    finally:
        await cleanup()


# --- company-managed toggle ---


async def _owner(session) -> User:
    """Another admin, who owns the dataset the first one toggles."""
    owner = User(email="o@x.io", full_name="Owner", hashed_password="x", role="admin")
    session.add(owner)
    await session.flush()
    await session.commit()
    return owner


async def _toggle(session, admin, dataset_id: str, **change) -> dict:
    return await datasets_api.update_dataset(
        dataset_id=dataset_id,
        body=UpdateDatasetRequest(**change),
        user=admin,
        db=session,
        _ai_gate=None,
    )


def _sharing(resp: dict) -> tuple:
    return (
        resp["is_company_managed"],
        resp["access_restricted"],
        resp["shared_user_ids"],
        resp["shared_group_ids"],
    )


async def test_a_dataset_made_company_managed_drops_its_grants_and_keeps_its_owner():
    """Visible to everyone, its grants confer nothing and are cleared; its owner
    stays, not the admin who toggled it."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, owner = await _admin(s), await _owner(s)
            user, group = await _grantees(s)
            created = await _create_dataset(s, owner, shared_user_ids=[user], shared_group_ids=[group])
            resp = await _toggle(s, admin, created["id"], is_company_managed=True)
            assert _sharing(resp) == (True, False, None, None)
        async with sm() as s:
            stored = await s.get(Dataset, uuid.UUID(created["id"]))
            assert stored.user_id == owner.id
    finally:
        await cleanup()


async def test_a_dataset_made_company_managed_and_restricted_keeps_its_grants():
    """Restricted, the grants become the audience: clearing them would publish it."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            user, group = await _grantees(s)
            created = await _create_dataset(s, admin, shared_user_ids=[user], shared_group_ids=[group])
            resp = await _toggle(s, admin, created["id"], is_company_managed=True, access_restricted=True)
            assert _sharing(resp) == (True, True, [user], [group])
    finally:
        await cleanup()


async def test_a_restricted_company_managed_dataset_made_private_keeps_its_grants():
    """Going private lifts the restriction, leaves the people it was shared with,
    and gives the dataset back to its owner."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, owner = await _admin(s), await _owner(s)
            user, group = await _grantees(s)
            created = await _create_dataset(
                s,
                owner,
                is_company_managed=True,
                access_restricted=True,
                shared_user_ids=[user],
                shared_group_ids=[group],
            )
            resp = await _toggle(s, admin, created["id"], is_company_managed=False)
            assert _sharing(resp) == (False, False, [user], [group])
        async with sm() as s:
            stored = await s.get(Dataset, uuid.UUID(created["id"]))
            assert stored.user_id == owner.id
    finally:
        await cleanup()


async def test_a_company_managed_dataset_with_no_owner_made_private_is_owned_by_the_admin():
    """A legacy row created without an owner gets one when it stops being everyone's."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, is_company_managed=True)
            stored = await s.get(Dataset, uuid.UUID(created["id"]))
            stored.user_id = None
            await s.commit()
            resp = await _toggle(s, admin, created["id"], is_company_managed=False)
            assert resp["is_company_managed"] is False
        async with sm() as s:
            stored = await s.get(Dataset, uuid.UUID(created["id"]))
            assert stored.user_id == admin.id
    finally:
        await cleanup()


# --- entry vocabulary validation ---


async def test_add_entry_rejects_label_outside_vocabulary():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s, admin, family="CLASSIFICATION", label_vocabulary=["spam", "ham"]
            )
            with pytest.raises(HTTPException) as exc:
                await datasets_api.add_entry(
                    dataset_id=created["id"],
                    body=DatasetEntryRequest(
                        input_prompt="q", expected_answer="phishing"
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "phishing" in exc.value.detail
    finally:
        await cleanup()


async def test_add_entry_accepts_label_within_vocabulary():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s, admin, family="CLASSIFICATION", label_vocabulary=["spam", "ham"]
            )
            entry = await datasets_api.add_entry(
                dataset_id=created["id"],
                body=DatasetEntryRequest(input_prompt="q", expected_answer="spam"),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert entry["expected_answer"] == "spam"
    finally:
        await cleanup()


_BUNDLE = '{"type": "bundle", "id": "bundle--1", "objects": []}'


@pytest.mark.parametrize(
    ("family", "answer", "updated_answer"),
    [("STIX_BUNDLE", _BUNDLE, _BUNDLE), ("PROSE", "anything goes here", "still anything")],
    ids=["STIX_BUNDLE", "PROSE"],
)
async def test_entries_unaffected_by_vocabulary_on_non_classification_family(
    family, answer, updated_answer
):
    """Entries on a family with no declared vocabulary are not label-checked."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family=family)
            entry = await datasets_api.add_entry(
                dataset_id=created["id"],
                body=DatasetEntryRequest(input_prompt="q", expected_answer=answer),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert entry["expected_answer"] == answer

            updated = await datasets_api.update_entry(
                entry_id=entry["id"],
                body=DatasetEntryRequest(input_prompt="q", expected_answer=updated_answer),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert updated["expected_answer"] == updated_answer
    finally:
        await cleanup()


async def test_narrowing_vocabulary_rejected_when_an_existing_entry_would_be_invalidated():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s, admin, family="CLASSIFICATION", label_vocabulary=["spam", "ham", "phishing"]
            )
            await datasets_api.add_entry(
                dataset_id=created["id"],
                body=DatasetEntryRequest(input_prompt="q", expected_answer="phishing"),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            with pytest.raises(HTTPException) as exc:
                await datasets_api.update_dataset(
                    dataset_id=created["id"],
                    body=UpdateDatasetRequest(label_vocabulary=["spam", "ham"]),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "phishing" in exc.value.detail
    finally:
        await cleanup()


# --- seed-ground-truth ---


async def test_seed_ground_truth_previews_an_independent_deep_copy():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON")
            schema = {"type": "object", "properties": {"a": {"type": "string"}}}
            agent = Agent(user_id=admin.id, name="Agent", output_schema=schema)
            s.add(agent)
            await s.commit()
            await s.refresh(agent)

            preview = await datasets_api.seed_ground_truth(
                dataset_id=created["id"],
                agent_id=str(agent.id),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert preview["found"] is True
            assert preview["output_schema"] == schema
            assert preview["output_schema"] is not agent.output_schema

            # Mutating the agent's schema afterwards never changes a taken preview.
            agent.output_schema["properties"]["a"]["type"] = "integer"
            assert preview["output_schema"]["properties"]["a"]["type"] == "string"
    finally:
        await cleanup()


@pytest.mark.parametrize("family", ["CLASSIFICATION", "PROSE", "STIX_BUNDLE"])
async def test_seed_ground_truth_reports_nothing_to_seed_for_other_families(family):
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family=family)
            agent = Agent(
                user_id=admin.id,
                name="Agent",
                output_schema={"type": "object"},
            )
            s.add(agent)
            await s.commit()
            await s.refresh(agent)

            preview = await datasets_api.seed_ground_truth(
                dataset_id=created["id"],
                agent_id=str(agent.id),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert preview["found"] is False
            assert "message" in preview
    finally:
        await cleanup()


# ── Export/import round-trips the scoring contract ────────────────────────────────────
# The family, output schema and label vocabulary travel in the manifest. Without
# them a re-imported dataset lands in ``needs_family`` and cannot be scored like the
# original.


SCHEMA = {
    "type": "json_schema",
    "definition": {"type": "object", "properties": {"verdict": {"type": "string"}}},
}
ENTRY_SCHEMA = {
    "type": "json_schema",
    "definition": {"type": "object", "properties": {"verdict": {"type": "number"}}},
}


async def _export(session, admin, dataset_id: str) -> bytes:
    response = await datasets_api.export_dataset(
        dataset_id=dataset_id, user=admin, db=session
    )
    chunks = [chunk async for chunk in response.body_iterator]
    return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)


def _manifest_of(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return json.loads(zf.read("manifest.json"))


def _zip_of(manifest: dict, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for path, data in (extra or {}).items():
            zf.writestr(path, data)
    return buf.getvalue()


async def _import(session, admin, zip_bytes: bytes) -> dict:
    return await datasets_api.import_dataset(
        file=UploadFile(filename="d.zip", file=io.BytesIO(zip_bytes)),
        user=admin,
        db=session,
        _ai_gate=None,
    )


# --- round trips ---------------------------------------------------------------


async def test_classification_round_trip_keeps_family_and_vocabulary():
    """A CLASSIFICATION dataset keeps its family and vocabulary."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s,
                admin,
                family="CLASSIFICATION",
                label_vocabulary=["spam", "ham"],
                entries=[
                    DatasetEntryRequest(input_prompt="p1", expected_answer='["spam"]'),
                    DatasetEntryRequest(input_prompt="p2", expected_answer='["ham"]'),
                ],
            )
            zip_bytes = await _export(s, admin, created["id"])

        manifest = _manifest_of(zip_bytes)
        assert manifest["dataset"]["family"] == "CLASSIFICATION"
        assert manifest["dataset"]["label_vocabulary"] == ["spam", "ham"]

        async with sm() as s:
            imported = await _import(s, admin, zip_bytes)

        assert imported["family"] == "CLASSIFICATION"
        assert imported["label_vocabulary"] == ["spam", "ham"]
        # Not left in the unresolved state.
        assert not imported.get("needs_family")
        assert [e["expected_answer"] for e in imported["entries"]] == [
            '["spam"]',
            '["ham"]',
        ]
    finally:
        await cleanup()


async def test_structured_json_round_trip_keeps_both_schema_levels():
    """The per-entry override is its own field, so it needs its own assertion."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s,
                admin,
                family="STRUCTURED_JSON",
                output_schema=SCHEMA,
                entries=[
                    DatasetEntryRequest(
                        input_prompt="p1", expected_answer='{"verdict": "yes"}'
                    ),
                    DatasetEntryRequest(
                        input_prompt="p2",
                        # Held to its own schema, not the dataset's.
                        expected_answer='{"verdict": 2}',
                        output_schema=ENTRY_SCHEMA,
                    ),
                ],
            )
            zip_bytes = await _export(s, admin, created["id"])

        manifest = _manifest_of(zip_bytes)
        assert manifest["dataset"]["output_schema"] == SCHEMA
        assert manifest["entries"][0]["output_schema"] is None
        assert manifest["entries"][1]["output_schema"] == ENTRY_SCHEMA

        async with sm() as s:
            imported = await _import(s, admin, zip_bytes)
            rows = (
                await s.execute(
                    select(DatasetEntry)
                    .where(DatasetEntry.dataset_id == uuid.UUID(imported["id"]))
                    .order_by(DatasetEntry.position)
                )
            ).scalars().all()

        assert imported["family"] == "STRUCTURED_JSON"
        assert imported["output_schema"] == SCHEMA
        assert [r.output_schema for r in rows] == [None, ENTRY_SCHEMA]
    finally:
        await cleanup()


async def test_the_manifest_declares_version_two():
    """``format`` stays v1 so older servers still import new packages; ``version``
    says which fields to expect."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="PROSE")
            manifest = _manifest_of(await _export(s, admin, created["id"]))

        assert manifest["format"] == "filigran-dataset-v1"
        assert manifest["version"] == 2
    finally:
        await cleanup()


# --- older packages must not start failing -----------------------------------


async def test_a_package_without_the_new_fields_still_imports():
    """A v1 package carries none of them and must land family-less, not error."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            imported = await _import(
                s,
                admin,
                _zip_of(
                    {
                        "format": "filigran-dataset-v1",
                        "version": 1,
                        "dataset": {"name": "legacy", "description": "d"},
                        "entries": [{"position": 0, "input_prompt": "p"}],
                        "files": [],
                    }
                ),
            )

        assert imported["name"] == "legacy"
        assert imported["family"] is None
        assert imported["output_schema"] is None
        assert imported["label_vocabulary"] is None
        assert len(imported["entries"]) == 1
    finally:
        await cleanup()


# --- a hand-edited manifest is held to the same rules as a create ------------


_V2_MANIFEST = {
    "format": "filigran-dataset-v1",
    "version": 2,
    "dataset": {"name": "x"},
    "entries": [{"position": 0, "input_prompt": "p"}],
    "files": [],
}


@pytest.mark.parametrize(
    ("manifest_over", "expected"),
    [
        ({"dataset": {"name": "x", "family": "NOT_A_FAMILY"}}, "Unknown dataset family"),
        (
            {"dataset": {"name": "x", "family": "PROSE", "label_vocabulary": ["a"]}},
            "only be set on a CLASSIFICATION dataset",
        ),
        (
            {"dataset": {"name": "x", "family": "PROSE", "output_schema": SCHEMA}},
            "only be set on a STRUCTURED_JSON dataset",
        ),
        (
            {"dataset": {"name": "x", "family": "CLASSIFICATION", "label_vocabulary": []}},
            "must be non-empty",
        ),
        # Wrong types: a string vocabulary must not persist as ['a','b','c'],
        # and a wrong-typed field is a 422, not a 500.
        (
            {"dataset": {"name": "x", "family": "CLASSIFICATION", "label_vocabulary": "abc"}},
            "must be a list of strings",
        ),
        (
            {"dataset": {"name": "x", "family": "CLASSIFICATION", "label_vocabulary": [1, 2]}},
            "must be a list of strings",
        ),
        ({"dataset": {"name": "x", "family": ["CLASSIFICATION"]}}, "family must be a string"),
        # Every value, not only the sections: each of these reached a KeyError, a
        # TypeError or the database as a 500.
        ({"dataset": {"name": "x" * 201}}, "manifest dataset: name: String should have at most 200 characters"),
        ({"dataset": {"name": None}}, "manifest dataset: name: Input should be a valid string"),
        (
            {"entries": [{"position": 0, "input_prompt": 5}]},
            "manifest entries[0]: input_prompt: Input should be a valid string",
        ),
        (
            {"entries": [{"position": 0, "expected_answer": ["a"]}]},
            "manifest entries[0]: expected_answer: Input should be a valid string",
        ),
        ({"entries": [{"position": 0, "files": "f0"}]}, "manifest entries[0]: files: Input should be a valid list"),
        ({"entries": [{"position": 0, "files": [["f0"]]}]}, "manifest entries[0]: files.0: Input should be a valid string"),
        ({"files": [{"path": "files/f0_a.txt"}]}, "manifest files[0]: id: Field required"),
        ({"files": [{"id": 0, "path": "files/f0_a.txt"}]}, "manifest files[0]: id: Input should be a valid string"),
        (
            {"files": [{"id": "f0", "path": "files/f0_a.txt", "filename": "a" * 501}]},
            "filename must be at most 500 characters",
        ),
        (
            {"files": [{"id": "f0", "path": "files/f0_a.txt", "content_type": "a" * 201}]},
            "content_type must be at most 200 characters",
        ),
        (
            {
                "dataset": {
                    "name": "x",
                    "family": "CLASSIFICATION",
                    "label_vocabulary": ["spam"],
                },
                "entries": [
                    {"position": 0, "input_prompt": "p", "expected_answer": '["phish"]'}
                ],
            },
            "phish",
        ),
        # A malformed container is a 422 naming the field, not a 500.
        ({"dataset": "not-an-object"}, "'dataset' must be an object"),
        ({"entries": {"0": {}}}, "'entries' must be a list of objects"),
        ({"entries": ["not-an-object"]}, "'entries' must be a list of objects"),
        ({"files": "nope"}, "'files' must be a list of objects"),
        # The evaluation import finds an entry by its position.
        ({"entries": [{"position": "first", "input_prompt": "p"}]}, "position must be a non-negative integer"),
    ],
    ids=[
        "unknown-family",
        "vocabulary-off-classification",
        "schema-off-structured-json",
        "empty-vocabulary",
        "string-vocabulary",
        "non-string-labels",
        "non-string-family",
        "name-past-its-column",
        "null-name",
        "non-string-prompt",
        "non-string-expected-answer",
        "entry-files-not-a-list",
        "unhashable-file-reference",
        "file-without-id",
        "non-string-file-id",
        "filename-past-its-column",
        "content-type-past-its-column",
        "label-outside-vocabulary",
        "dataset-not-an-object",
        "entries-not-a-list",
        "entry-not-an-object",
        "files-not-a-list",
        "non-integer-position",
    ],
)
async def test_a_manifest_the_create_path_would_refuse_is_refused(
    manifest_over: dict, expected: str
):
    """Import must not be a back door creating rows no other endpoint could."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(s, admin, _zip_of({**_V2_MANIFEST, **manifest_over}))
        assert exc.value.status_code == 422
        assert expected in str(exc.value.detail)
    finally:
        await cleanup()


async def test_an_entry_with_a_null_position_takes_its_place_in_the_list():
    """``null`` was written to the NOT NULL column, a 500."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            imported = await _import(
                s,
                admin,
                _zip_of(
                    {
                        **_V2_MANIFEST,
                        "entries": [
                            {"position": None, "input_prompt": "first"},
                            {"position": None, "input_prompt": "second"},
                        ],
                    }
                ),
            )
        assert [(e["input_prompt"], e["position"]) for e in imported["entries"]] == [("first", 0), ("second", 1)]
    finally:
        await cleanup()


async def test_tied_positions_are_renumbered_in_their_list_order():
    """An older exported dataset can carry ties: no result names its entries
    here, so the import renumbers them rather than refusing or storing the tie."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            imported = await _import(
                s,
                admin,
                _zip_of(
                    {
                        **_V2_MANIFEST,
                        "entries": [
                            {"position": 0, "input_prompt": "first"},
                            {"position": 0, "input_prompt": "second"},
                            {"position": 5, "input_prompt": "fourth"},
                            {"position": 1, "input_prompt": "third"},
                        ],
                    }
                ),
            )
        assert [(e["input_prompt"], e["position"]) for e in imported["entries"]] == [
            ("first", 0),
            ("second", 1),
            ("third", 2),
            ("fourth", 3),
        ]
    finally:
        await cleanup()


async def test_a_package_too_large_uncompressed_is_refused(monkeypatch):
    """What the members declare once uncompressed is capped, as the upload is."""
    package = _zip_of(_V2_MANIFEST)
    declared = sum(member.file_size for member in zipfile.ZipFile(io.BytesIO(package)).infolist())
    monkeypatch.setattr(datasets_api, "MAX_UNCOMPRESSED_SIZE", declared - 1)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(s, admin, package)
        assert exc.value.status_code == 413
        assert "uncompressed" in exc.value.detail
    finally:
        await cleanup()


async def test_a_file_declaring_less_than_it_holds_is_refused_without_being_held(monkeypatch):
    """A member's declared size is set by whoever built the package: a file
    declaring 10 bytes and holding 64 MiB is refused as corrupt, having decompressed
    what it declared, where reading it whole held it twice over."""
    from tests.archive_helpers import declare_size, peak_mib_of

    stored: list[str] = []

    async def _upload(**kwargs):
        stored.append(kwargs["filename"])
        return "object"

    monkeypatch.setattr("app.services.file_storage.file_storage.upload", _upload)
    package = declare_size(
        _zip_of(
            {**_V2_MANIFEST, "files": [{"id": "f0", "path": "files/f0.bin", "filename": "f0.bin"}]},
            {"files/f0.bin": b"\0" * (64 * 2**20)},
        ),
        "files/f0.bin",
        10,
    )
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            peak, refused = await peak_mib_of(_import(s, admin, package))
        assert isinstance(refused, HTTPException)
        assert refused.status_code == 400
        assert refused.detail == "Invalid ZIP file"
        assert peak <= 32
        assert stored == []
    finally:
        await cleanup()


async def test_a_manifest_declaring_less_than_it_holds_is_an_invalid_zip_file():
    """Refused as the corrupt archive it is, not as JSON it never got to be."""
    from tests.archive_helpers import declare_size

    package = declare_size(_zip_of(_V2_MANIFEST), "manifest.json", 10)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(s, admin, package)
        assert exc.value.status_code == 400
        assert exc.value.detail == "Invalid ZIP file"
    finally:
        await cleanup()


async def test_a_file_read_past_the_package_budget_is_a_413_and_leaves_nothing_stored(monkeypatch):
    """The budget is spent on what is read: one file listed twice is read twice.
    The copy stored before the refusal is deleted with the transaction."""
    stored: list[str] = []
    deleted: list[str] = []

    async def _upload(**_kwargs):
        stored.append(f"dataset/import/{uuid.uuid4().hex}")
        return stored[-1]

    async def _delete(object_name):
        deleted.append(object_name)

    monkeypatch.setattr("app.services.file_storage.file_storage.upload", _upload)
    monkeypatch.setattr("app.services.file_storage.file_storage.delete", _delete)
    package = _zip_of(
        {
            **_V2_MANIFEST,
            "files": [
                {"id": "f0", "path": "files/f0.bin", "filename": "a.bin"},
                {"id": "f1", "path": "files/f0.bin", "filename": "b.bin"},
            ],
        },
        {"files/f0.bin": b"x" * 1000},
    )
    declared = sum(member.file_size for member in zipfile.ZipFile(io.BytesIO(package)).infolist())
    monkeypatch.setattr(datasets_api, "MAX_UNCOMPRESSED_SIZE", declared + 10)
    from tests.evaluation_database import _make_db_full

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(s, admin, package)
        assert exc.value.status_code == 413
        assert "files/f0.bin" in exc.value.detail
        assert len(stored) == 1
        assert deleted == stored
    finally:
        await cleanup()


async def test_a_manifest_past_the_json_member_cap_is_a_413(monkeypatch):
    """A JSON member is parsed whole, so it has a cap of its own within the budget."""
    monkeypatch.setattr(datasets_api, "MAX_JSON_MEMBER_SIZE", 10)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(s, admin, _zip_of(_V2_MANIFEST))
        assert exc.value.status_code == 413
        assert "manifest.json" in exc.value.detail
    finally:
        await cleanup()


async def test_an_upload_past_the_package_limit_is_refused_having_read_one_byte_past_it(monkeypatch):
    """The upload is read up to the limit and one byte, never whole."""
    monkeypatch.setattr(datasets_api, "MAX_ZIP_SIZE", 1024)
    upload = io.BytesIO(b"x" * 10_000)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await datasets_api.import_dataset(
                    file=UploadFile(filename="d.zip", file=upload), user=admin, db=s, _ai_gate=None
                )
        assert exc.value.status_code == 413
        assert upload.tell() == 1025
    finally:
        await cleanup()


async def test_a_refused_manifest_uploads_nothing(monkeypatch):
    """The contract check runs before the uploads, so a refusal leaves no blob."""
    uploads: list[str] = []

    async def _record_upload(**kwargs):
        uploads.append(kwargs.get("filename", ""))
        return "s3://never"

    from app.services import file_storage as file_storage_module

    monkeypatch.setattr(file_storage_module.file_storage, "upload", _record_upload)

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _import(
                    s,
                    admin,
                    _zip_of(
                        {
                            "format": "filigran-dataset-v1",
                            "version": 2,
                            "dataset": {"name": "x", "family": "NOT_A_FAMILY"},
                            "entries": [{"position": 0, "input_prompt": "p"}],
                            "files": [
                                {
                                    "id": "f0",
                                    "filename": "a.txt",
                                    "content_type": "text/plain",
                                    "size": 2,
                                    "path": "files/f0_a.txt",
                                }
                            ],
                        },
                        extra={"files/f0_a.txt": b"hi"},
                    ),
                )
        assert exc.value.status_code == 422
        assert uploads == []
    finally:
        await cleanup()


# --- an expected answer the scorer cannot read is refused at save ---


@pytest.mark.parametrize("operation", ["add", "update", "create", "import", "family_change"])
async def test_a_stix_reference_that_is_not_a_json_object_is_refused(operation):
    """The run would leave the entry out for a reason unrelated to the agent."""
    not_a_bundle = '["indicator"]'
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            entries = [DatasetEntryRequest(input_prompt="q", expected_answer=_BUNDLE)]
            created = await _create_dataset(s, admin, family="STIX_BUNDLE", entries=entries)
            with pytest.raises(HTTPException) as exc:
                if operation == "add":
                    await datasets_api.add_entry(
                        dataset_id=created["id"],
                        body=DatasetEntryRequest(input_prompt="q", expected_answer=not_a_bundle),
                        user=admin,
                        db=s,
                        _ai_gate=None,
                    )
                elif operation == "update":
                    await datasets_api.update_entry(
                        entry_id=created["entries"][0]["id"],
                        body=DatasetEntryRequest(input_prompt="q", expected_answer=not_a_bundle),
                        user=admin,
                        db=s,
                        _ai_gate=None,
                    )
                elif operation == "create":
                    await _create_dataset(
                        s,
                        admin,
                        name="other",
                        family="STIX_BUNDLE",
                        entries=[DatasetEntryRequest(input_prompt="q", expected_answer=not_a_bundle)],
                    )
                elif operation == "family_change":
                    prose = await _create_dataset(
                        s,
                        admin,
                        name="prose",
                        family="PROSE",
                        entries=[DatasetEntryRequest(input_prompt="q", expected_answer=not_a_bundle)],
                    )
                    await datasets_api.update_dataset(
                        dataset_id=prose["id"],
                        body=UpdateDatasetRequest(family="STIX_BUNDLE"),
                        user=admin,
                        db=s,
                        _ai_gate=None,
                    )
                else:
                    manifest = _manifest_of(await _export(s, admin, created["id"]))
                    manifest["entries"][0]["expected_answer"] = not_a_bundle
                    await _import(s, admin, _zip_of(manifest))
            assert exc.value.status_code == 422
            assert "cannot be scored" in exc.value.detail
    finally:
        await cleanup()


async def test_an_entry_with_its_own_schema_is_held_to_it_not_the_dataset_s():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STRUCTURED_JSON", output_schema=SCHEMA)
            with pytest.raises(HTTPException) as exc:
                await datasets_api.add_entry(
                    dataset_id=created["id"],
                    body=DatasetEntryRequest(
                        input_prompt="q", expected_answer='{"verdict": "yes"}', output_schema=ENTRY_SCHEMA
                    ),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "/verdict" in exc.value.detail
    finally:
        await cleanup()


async def test_a_dataset_schema_change_does_not_check_entries_with_their_own():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(
                s,
                admin,
                family="STRUCTURED_JSON",
                entries=[
                    DatasetEntryRequest(
                        input_prompt="q", expected_answer='{"verdict": 2}', output_schema=ENTRY_SCHEMA
                    )
                ],
            )
            updated = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(output_schema=SCHEMA),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert updated["output_schema"] == SCHEMA
    finally:
        await cleanup()


async def test_a_save_that_keeps_the_stix_family_never_fails_on_an_entry_written_before():
    """Only a switch to STIX_BUNDLE checks the entries: a read-only dataset holding an
    entry written before the check must still unlock, or its entries could never be fixed."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create_dataset(s, admin, family="STIX_BUNDLE", is_read_only=True)
            s.add(
                DatasetEntry(
                    dataset_id=uuid.UUID(created["id"]), position=0, expected_answer="not a bundle"
                )
            )
            await s.commit()

            updated = await datasets_api.update_dataset(
                dataset_id=created["id"],
                body=UpdateDatasetRequest(family="STIX_BUNDLE", is_read_only=False),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert (updated["family"], updated["is_read_only"]) == ("STIX_BUNDLE", False)
    finally:
        await cleanup()
