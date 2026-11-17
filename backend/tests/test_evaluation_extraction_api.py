"""Save-time validation and the read-only preview endpoint.

The pure-validation half needs no database. The route half uses the throwaway-database
harness and calls the route functions directly, as ``test_evaluations.py`` does; there is
no ``AsyncClient`` fixture in this repo.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.evaluation_extractors import (
    validate_agent_extractors,
    validate_existing_extractors_against_family,
)
from app.api.v1.schemas.evaluation import PreviewExtractorRequest
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationEpisode,
    EvaluationResult,
    EvaluationResultTrace,
    EvaluationRun,
)
from app.models.flow import Flow
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.services.evaluation.extraction.limits import current_limits
from tests.fixtures.extraction import adverse, traces
from tests.scratch_database import scratch_database

LABEL_CHAIN: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "object_values"},
        {"op": "flatten"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}

JSON_CHAIN: dict[str, Any] = {
    "source": {"kind": "tool_output", "tool_name": traces.TOOL_NAME},
    "operations": [{"op": "parse_json"}],
    "output_kind": "object",
}


# ---------------------------------------------------------------------------
# Save-time validation — pure, no database
# ---------------------------------------------------------------------------


def test_an_agent_key_not_on_the_evaluation_is_refused() -> None:
    problem = validate_agent_extractors(
        {"11111111-1111-1111-1111-111111111111": LABEL_CHAIN},
        family="CLASSIFICATION",
        known_agent_ids={"22222222-2222-2222-2222-222222222222"},
    )
    assert problem is not None and "not on this evaluation" in problem


def test_a_family_change_that_would_orphan_an_extractor_is_refused() -> None:
    row = EvaluationAgent(
        id=uuid.uuid4(),
        evaluation_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        output_extractor=LABEL_CHAIN,
    )
    problem = validate_existing_extractors_against_family([row], "STRUCTURED_JSON")
    assert problem is not None and str(row.agent_id) in problem


def test_a_null_extractor_clears_rather_than_validating() -> None:
    agent_id = "33333333-3333-3333-3333-333333333333"
    assert (
        validate_agent_extractors(
            {agent_id: None}, family="CLASSIFICATION", known_agent_ids={agent_id}
        )
        is None
    )


# ---------------------------------------------------------------------------
# Preview route — throwaway database, direct route calls
# ---------------------------------------------------------------------------

_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
    EvaluationResult.__table__,
    EvaluationResultTrace.__table__,
    # ``get_run`` reads the episodes to ask whether a late callback can still
    # change this run's headlines, so the table must exist.
    EvaluationEpisode.__table__,
    OrchestrationExecution.__table__,
    AuditLog.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationDataset.__table__,
]


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("extract", tables=_TABLES))
    return db.sessions, stack.aclose


@pytest.fixture(autouse=True)
def _stub_groups(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)


async def _seed(session, *, family: str = "CLASSIFICATION"):
    admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    agent = Agent(name="Harvester", slug=f"harvester-{uuid.uuid4().hex[:8]}")
    session.add(agent)
    evaluation = Evaluation(name="Extraction", family=family)
    session.add(evaluation)
    await session.flush()
    session.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
    await session.commit()
    return admin, evaluation, agent.id


def _request(agent_id, extractor, **kwargs) -> PreviewExtractorRequest:
    return PreviewExtractorRequest(
        agent_id=str(agent_id), extractor=extractor, **kwargs
    )


@pytest.mark.integration
async def test_preview_requires_exactly_one_material_source() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            for kwargs in ({}, {"result_id": str(uuid.uuid4()), "sample": {}}):
                with pytest.raises(HTTPException) as exc:
                    await evaluations_api.preview_extractor(
                        evaluation_id=str(evaluation.id),
                        body=_request(agent_id, LABEL_CHAIN, **kwargs),
                        user=admin,
                        db=s,
                    )
                assert exc.value.status_code == 422
                assert "exactly one" in exc.value.detail
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_against_a_pasted_sample() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            result = await evaluations_api.preview_extractor(
                evaluation_id=str(evaluation.id),
                body=_request(
                    agent_id,
                    LABEL_CHAIN,
                    sample={"agent_output": adverse.harvester_merged()},
                ),
                user=admin,
                db=s,
            )
            assert result["status"] == "extracted"
            assert result["prediction"] == (
                '["T1003.001","T1053.005","T1059.001","T1105","T1574.002"]'
            )
            assert result["item_count"] == 5
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_reports_a_typed_failure() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            result = await evaluations_api.preview_extractor(
                evaluation_id=str(evaluation.id),
                body=_request(
                    agent_id, LABEL_CHAIN, sample={"agent_output": "not json"}
                ),
                user=admin,
                db=s,
            )
            assert result["status"] == "failed"
            assert result["failure"]["reason"] == "payload_unparseable"
            assert result["prediction"] is None
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_refuses_an_agent_not_on_the_evaluation() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, _ = await _seed(s)
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        uuid.uuid4(), LABEL_CHAIN, sample={"agent_output": "{}"}
                    ),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 404
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_refuses_a_result_from_another_evaluation_or_agent() -> None:
    """Resolved by JOIN: a result id alone must never be enough."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            other = Evaluation(name="Other", family="CLASSIFICATION")
            other_agent = Agent(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
            s.add_all([other, other_agent])
            await s.flush()
            foreign_run = EvaluationRun(
                evaluation_id=other.id,
                agent_id=other_agent.id,
                model_id="m",
                status="completed",
                trigger_reason="manual",
            )
            s.add(foreign_run)
            await s.flush()
            foreign_result = EvaluationResult(
                run_id=foreign_run.id, agent_output="{}", score=0.0, sub_scores={}
            )
            s.add(foreign_result)
            await s.commit()

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id, LABEL_CHAIN, result_id=str(foreign_result.id)
                    ),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 404
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_refuses_an_incompatible_family() -> None:
    """The family check runs at preview, not only at save and run."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s, family="STRUCTURED_JSON")
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id, LABEL_CHAIN, sample={"agent_output": "{}"}
                    ),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 422
            assert "not supported" in exc.value.detail
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_persists_nothing() -> None:
    from sqlalchemy import func, select

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)

            async def counts() -> tuple[int, int]:
                runs = await s.execute(select(func.count()).select_from(EvaluationRun))
                results = await s.execute(
                    select(func.count()).select_from(EvaluationResult)
                )
                return int(runs.scalar_one()), int(results.scalar_one())

            before = await counts()
            for _ in range(10):
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id, LABEL_CHAIN, sample={"agent_output": "{}"}
                    ),
                    user=admin,
                    db=s,
                )
            assert await counts() == before
    finally:
        await cleanup()


@pytest.mark.integration
async def test_no_payload_reaches_errors_or_logs(caplog) -> None:
    """Only bounded metadata reaches the response and the log, never the payload."""
    sentinel = "SECRET-CANARY-8f2a"
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            with caplog.at_level("DEBUG"):
                result = await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id,
                        LABEL_CHAIN,
                        sample={"agent_output": f"{sentinel} is not json"},
                    ),
                    user=admin,
                    db=s,
                )
            assert sentinel not in repr(result)
            assert all(
                sentinel not in record.getMessage() for record in caplog.records
            )
    finally:
        await cleanup()


@pytest.mark.integration
async def test_extractor_add_edit_and_remove() -> None:
    """The full extractor lifecycle, read back from storage at every step."""
    from sqlalchemy import select

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            _, evaluation, agent_id = await _seed(s)

            async def stored() -> Any:
                row = await s.execute(
                    select(EvaluationAgent.output_extractor).where(
                        EvaluationAgent.evaluation_id == evaluation.id,
                        EvaluationAgent.agent_id == agent_id,
                    )
                )
                return row.scalar_one()

            from app.api.v1.evaluation_extractors import apply_agent_extractors

            assert await stored() is None

            await apply_agent_extractors(
                s, evaluation.id, {str(agent_id): LABEL_CHAIN}
            )
            await s.commit()
            assert await stored() == LABEL_CHAIN

            edited = dict(LABEL_CHAIN, operations=LABEL_CHAIN["operations"][:2])
            edited["output_kind"] = "array"
            await apply_agent_extractors(s, evaluation.id, {str(agent_id): edited})
            await s.commit()
            assert await stored() == edited

            await apply_agent_extractors(s, evaluation.id, {str(agent_id): None})
            await s.commit()
            # Removal restores raw-output scoring; it never stores an empty extractor.
            assert await stored() is None
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Tenancy and authorization
#
# The tests above authenticate as the evaluation's own admin. These drive the two
# guards that stand between a preview and another tenant's material.
# ---------------------------------------------------------------------------


async def _seed_second_user(session, *, role: str = "user") -> User:
    other = User(
        email=f"b-{uuid.uuid4().hex[:8]}@x.io",
        full_name="Other",
        hashed_password="x",
        role=role,
    )
    session.add(other)
    await session.commit()
    return other


@pytest.mark.integration
async def test_preview_is_not_visible_to_a_user_who_does_not_own_the_evaluation() -> None:
    """A stranger gets 404, not 403: the endpoint must not confirm existence."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            owner, evaluation, agent_id = await _seed(s)
            evaluation.user_id = owner.id  # owned by somebody else
            await s.commit()
            stranger = await _seed_second_user(s)

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id, LABEL_CHAIN, sample={"agent_output": "{}"}
                    ),
                    user=stranger,
                    db=s,
                )
            assert exc.value.status_code == 404
    finally:
        await cleanup()


@pytest.mark.integration
async def test_preview_on_a_company_managed_evaluation_requires_an_admin() -> None:
    """The company-managed guard is reached by preview, not only by save."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            _, evaluation, agent_id = await _seed(s)
            evaluation.is_company_managed = True
            evaluation.user_id = None
            await s.commit()
            non_admin = await _seed_second_user(s)
            assert non_admin.is_admin is False

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(
                        agent_id, LABEL_CHAIN, sample={"agent_output": "{}"}
                    ),
                    user=non_admin,
                    db=s,
                )
            assert exc.value.status_code in (403, 404)
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Stored-result listing
# ---------------------------------------------------------------------------


async def _seed_result(session, evaluation, agent_id, *, output: str = "{}"):
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent_id,
        model_id="m",
        status="completed",
        trigger_reason="manual",
    )
    session.add(run)
    await session.flush()
    result = EvaluationResult(
        run_id=run.id, agent_output=output, score=0.0, sub_scores={}
    )
    session.add(result)
    await session.commit()
    return result


@pytest.mark.integration
async def test_sample_results_lists_only_this_evaluations_results() -> None:
    """The listing returns this evaluation's results only, for previewing against one."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            mine = await _seed_result(s, evaluation, agent_id)

            other = Evaluation(name="Other", family="CLASSIFICATION")
            other_agent = Agent(name="O", slug=f"o-{uuid.uuid4().hex[:8]}")
            s.add_all([other, other_agent])
            await s.flush()
            foreign = await _seed_result(s, other, other_agent.id)

            rows = await evaluations_api.list_extractor_sample_results(
                evaluation_id=str(evaluation.id),
                agent_id=str(agent_id),
                user=admin,
                db=s,
            )
            ids = {row["result_id"] for row in rows}
            assert str(mine.id) in ids
            assert str(foreign.id) not in ids
    finally:
        await cleanup()


@pytest.mark.integration
async def test_sample_results_name_the_entry_with_data_not_an_english_label() -> None:
    """The reader's language is the frontend's: the listing gives what names the entry,
    its description or else its position, never a sentence."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            dataset = Dataset(name="ds")
            s.add(dataset)
            await s.flush()
            described = DatasetEntry(dataset_id=dataset.id, position=0, description="Phishing mail", expected_answer="a")
            plain = DatasetEntry(dataset_id=dataset.id, position=4, expected_answer="b")
            s.add_all([described, plain])
            await s.flush()
            for entry in (described, plain):
                result = await _seed_result(s, evaluation, agent_id)
                result.entry_id = entry.id
            await s.commit()

            rows = await evaluations_api.list_extractor_sample_results(
                evaluation_id=str(evaluation.id),
                agent_id=str(agent_id),
                user=admin,
                db=s,
            )
            assert {(row["description"], row["position"]) for row in rows} == {
                ("Phishing mail", 0),
                (None, 4),
            }
    finally:
        await cleanup()


@pytest.mark.integration
async def test_sample_results_never_returns_agent_output() -> None:
    """Listing must not become a way to read outputs it only labels."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            await _seed_result(s, evaluation, agent_id, output="SECRET-PAYLOAD")

            rows = await evaluations_api.list_extractor_sample_results(
                evaluation_id=str(evaluation.id),
                agent_id=str(agent_id),
                user=admin,
                db=s,
            )
            assert rows
            for row in rows:
                assert set(row) == {"result_id", "created_at", "description", "position"}
                assert "SECRET-PAYLOAD" not in str(row)
    finally:
        await cleanup()


@pytest.mark.integration
async def test_a_listed_result_is_actually_previewable() -> None:
    """The listing and the preview agree, so a picked row always works."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            await _seed_result(s, evaluation, agent_id, output=adverse.harvester_merged())

            rows = await evaluations_api.list_extractor_sample_results(
                evaluation_id=str(evaluation.id),
                agent_id=str(agent_id),
                user=admin,
                db=s,
            )
            result = await evaluations_api.preview_extractor(
                evaluation_id=str(evaluation.id),
                body=_request(agent_id, LABEL_CHAIN, result_id=rows[0]["result_id"]),
                user=admin,
                db=s,
            )
            assert result["status"] == "extracted"
            assert result["item_count"] == 5
    finally:
        await cleanup()


@pytest.mark.integration
async def test_sample_results_is_refused_to_a_stranger() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            owner, evaluation, agent_id = await _seed(s)
            evaluation.user_id = owner.id
            await s.commit()
            stranger = await _seed_second_user(s)

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.list_extractor_sample_results(
                    evaluation_id=str(evaluation.id),
                    agent_id=str(agent_id),
                    user=stranger,
                    db=s,
                )
            assert exc.value.status_code == 404
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Query cost of the run-level count
#
# Measured on the statements the ROUTE issues: their number must not grow with
# the number of failures it reports.
# ---------------------------------------------------------------------------


class _StatementCounter:
    """Counts SQL statements issued on a session's connection."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, _conn, _cursor, statement, *_rest) -> None:
        self.statements.append(statement)


async def _seed_run_with_results(session, evaluation, agent_id, *, failures: int, total: int):
    from app.services.evaluation.extraction.failures import (
        ExtractionFailure,
        failure_metric_blob,
    )

    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent_id,
        model_id="m",
        status="completed",
        trigger_reason="manual",
    )
    session.add(run)
    await session.flush()
    for index in range(total):
        failed = index < failures
        details: dict = {"metric_results": {}}
        if failed:
            details["metric_results"].update(
                failure_metric_blob(
                    ExtractionFailure(
                        reason="selection_empty", message="nothing selected"
                    ),
                    agent_id=str(agent_id),
                )
            )
        session.add(
            EvaluationResult(
                run_id=run.id,
                agent_output="{}",
                score=0.0,
                sub_scores={},
                execution_details=details,
            )
        )
    await session.commit()
    return run


async def _count_get_run_statements(sessionmaker, run_id, admin) -> int:
    from sqlalchemy import event

    counter = _StatementCounter()
    async with sessionmaker() as s:
        sync_engine = s.get_bind()
        event.listen(sync_engine, "before_cursor_execute", counter)
        try:
            await evaluations_api.get_run(run_id=str(run_id), user=admin, db=s)
        finally:
            event.remove(sync_engine, "before_cursor_execute", counter)
    return len(counter.statements)


@pytest.mark.integration
async def test_the_run_level_failure_count_costs_no_additional_query() -> None:
    """Reporting failures must not add a statement per failure."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            clean = await _seed_run_with_results(s, evaluation, agent_id, failures=0, total=6)
            dirty = await _seed_run_with_results(s, evaluation, agent_id, failures=6, total=6)

        clean_statements = await _count_get_run_statements(sm, clean.id, admin)
        dirty_statements = await _count_get_run_statements(sm, dirty.id, admin)

        # Guard against a vacuous 0 == 0.
        assert clean_statements > 0
        # The derivation is an in-memory pass: six failures cost what zero cost.
        assert clean_statements == dirty_statements

        async with sm() as s:
            resp = await evaluations_api.get_run(run_id=str(dirty.id), user=admin, db=s)
        assert resp["extraction_failure_count"] == 6
    finally:
        await cleanup()


@pytest.mark.integration
async def test_the_failure_blob_reaches_the_api_response() -> None:
    """The extraction failure record reaches the API response."""
    from app.services.evaluation.extraction.failures import EXTRACTION_FAILED_KEY

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            run = await _seed_run_with_results(s, evaluation, agent_id, failures=1, total=3)

        async with sm() as s:
            resp = await evaluations_api.get_run(run_id=str(run.id), user=admin, db=s)

        assert resp["extraction_failure_count"] == 1
        blobs = [
            r["execution_details"]["metric_results"][EXTRACTION_FAILED_KEY]
            for r in resp["results"]
            if EXTRACTION_FAILED_KEY in (r["execution_details"] or {}).get("metric_results", {})
        ]
        assert len(blobs) == 1
        blob = blobs[0]
        assert blob["error"]["reason"] == "selection_empty"
        assert blob["error"]["message"] == "nothing selected"
        # Metadata only: no score, so it cannot enter an aggregate.
        assert "score" not in blob
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Atomic family + extractors change
#
# Changing the family and fixing the extractors in one PATCH is the migration path;
# checking the STORED extractors against the new family would refuse it.
# ---------------------------------------------------------------------------


OBJECT_CHAIN = {
    "source": {"kind": "agent_output"},
    "operations": [{"op": "parse_json"}],
    "output_kind": "object",
}


@pytest.mark.integration
async def test_family_and_extractors_change_together_in_one_request() -> None:
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s, family="CLASSIFICATION")
            # The agent currently holds a label_set extractor, valid only for
            # CLASSIFICATION.
            await evaluations_api.apply_agent_extractors(
                s, evaluation.id, {str(agent_id): LABEL_CHAIN}
            )
            await s.commit()

            await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation.id),
                body=UpdateEvaluationRequest(
                    family="STRUCTURED_JSON",
                    agent_extractors={str(agent_id): OBJECT_CHAIN},
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )

            stored = await evaluations_api.load_agent_extractors(s, evaluation.id)
            assert stored[str(agent_id)]["output_kind"] == "object"
    finally:
        await cleanup()


@pytest.mark.integration
async def test_family_change_alone_still_refuses_an_orphaned_extractor() -> None:
    """The guard still bites when the request does not replace the extractor."""
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s, family="CLASSIFICATION")
            await evaluations_api.apply_agent_extractors(
                s, evaluation.id, {str(agent_id): LABEL_CHAIN}
            )
            await s.commit()

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.update_evaluation(
                    evaluation_id=str(evaluation.id),
                    body=UpdateEvaluationRequest(family="STRUCTURED_JSON"),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "unsupported" in exc.value.detail
    finally:
        await cleanup()


@pytest.mark.integration
async def test_applying_an_unlinked_agent_raises_rather_than_no_op() -> None:
    """A silent continue would return 200 having persisted nothing."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            _, evaluation, _agent_id = await _seed(s)
            with pytest.raises(ValueError, match="not linked"):
                await evaluations_api.apply_agent_extractors(
                    s, evaluation.id, {str(uuid.uuid4()): LABEL_CHAIN}
                )
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Preview sample bound
#
# The bound keeps preview cost predictable, so it weighs the whole sample: a
# tool_output extractor reads its payload out of ``traces``, not ``agent_output``.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_preview_weighs_the_whole_sample_not_only_the_answer() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s, family="STRUCTURED_JSON")
            bound = current_limits().max_sample_bytes
            # No agent_output at all: the payload lives entirely in the traces.
            fat_traces = [traces.tool_trace(tool_output="x" * (bound + 1024))]
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.preview_extractor(
                    evaluation_id=str(evaluation.id),
                    body=_request(agent_id, JSON_CHAIN, sample={"traces": fat_traces}),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 422
            assert str(bound) in exc.value.detail

            # A sample comfortably under the bound still previews.
            ok = await evaluations_api.preview_extractor(
                evaluation_id=str(evaluation.id),
                body=_request(
                    agent_id,
                    JSON_CHAIN,
                    sample={"traces": [traces.tool_trace(tool_output='{"a": 1}')]},
                ),
                user=admin,
                db=s,
            )
            assert ok["status"] == "extracted"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["tool_output", "tool_input"])
async def test_a_pasted_tool_sample_previews_in_the_shape_the_card_sends(kind) -> None:
    """The extractor card sends a tool source's pasted text as ONE successful call of the
    tool, the step ``previewSample`` builds; that exact shape extracts, for the
    output and the input alike, and the card has nothing else to know."""
    extractor = {
        "source": {"kind": kind, "tool_name": "search_cases"},
        "operations": [{"op": "parse_json"}],
        "output_kind": "object",
    }
    card_step = {"type": "tool", "tool_name": "search_cases", "outcome": "success", kind: '{"cases": [1]}'}
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s, family="STRUCTURED_JSON")
            preview = await evaluations_api.preview_extractor(
                evaluation_id=str(evaluation.id),
                body=_request(agent_id, extractor, sample={"traces": [card_step]}),
                user=admin,
                db=s,
            )
    finally:
        await cleanup()

    assert preview["status"] == "extracted"
    assert json.loads(preview["prediction"]) == {"cases": [1]}


# ---------------------------------------------------------------------------
# Extractor edits are not retroactive
#
# An extractor is not versioned and not snapshotted onto a run: editing one
# changes what the NEXT run or re-score computes, and leaves results already
# recorded exactly as they are.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_editing_an_extractor_does_not_alter_results_already_recorded() -> None:
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin, evaluation, agent_id = await _seed(s)
            await evaluations_api.apply_agent_extractors(
                s, evaluation.id, {str(agent_id): LABEL_CHAIN}
            )
            run = await _seed_run_with_results(s, evaluation, agent_id, failures=1, total=3)

        async with sm() as s:
            before = await evaluations_api.get_run(run_id=str(run.id), user=admin, db=s)

        async with sm() as s:
            edited = {**LABEL_CHAIN, "operations": [{"op": "parse_json"}, {"op": "object_keys"}, {"op": "dedupe_sort"}]}
            await evaluations_api.apply_agent_extractors(
                s, evaluation.id, {str(agent_id): edited}
            )
            await s.commit()

        async with sm() as s:
            after = await evaluations_api.get_run(run_id=str(run.id), user=admin, db=s)
            stored = await evaluations_api.load_agent_extractors(s, evaluation.id)

        # The definition changed for the next run...
        assert stored[str(agent_id)]["operations"] == edited["operations"]
        # ...and nothing already recorded moved.
        assert after["extraction_failure_count"] == before["extraction_failure_count"]
        assert [r["execution_details"] for r in after["results"]] == [
            r["execution_details"] for r in before["results"]
        ]
    finally:
        await cleanup()
