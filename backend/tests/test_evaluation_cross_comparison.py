"""Comparing evaluations of one family on their shared entries, and the STIX bundle review
(#12282): ``GET /evaluations/compare``, ``GET /evaluations/bundle-review``,
``services/evaluation/cross_comparison.py``, ``services/evaluation/bundle_review.py`` and
the seam's ``reference_key`` / ``canonical_stix_bundle``."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api.v1.evaluations import compare_evaluations_route, get_bundle_review, get_bundle_reviews
from app.api.v1.schemas.evaluation import BundleReviewResponse, BundleReviewsRequest, EvaluationComparisonResponse
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.answer_facts import is_unreadable, judge_score, metric_counts, setup_of
from app.services.evaluation.bundle_review import EXPECTED, build_review
from app.services.evaluation.metric_package import canonical_stix_bundle, reference_key
from tests.evaluation_database import _admin, _make_db, _stub_audit_and_groups  # noqa: F401

MICRO = "classification.micro_prf"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _bundle(*objects: dict) -> str:
    return json.dumps({"type": "bundle", "id": f"bundle--{uuid.uuid4()}", "objects": list(objects)})


def _sdo(kind: str, name: str, **extra) -> dict:
    return {"type": kind, "spec_version": "2.1", "id": f"{kind}--{uuid.uuid4()}", "name": name,
            "created": "2026-01-01T00:00:00.000Z", "modified": "2026-01-01T00:00:00.000Z", **extra}


def _rel(source: dict, kind: str, target: dict) -> dict:
    return {"type": "relationship", "spec_version": "2.1", "id": f"relationship--{uuid.uuid4()}",
            "relationship_type": kind, "source_ref": source["id"], "target_ref": target["id"],
            "created": "2026-01-01T00:00:00.000Z", "modified": "2026-01-01T00:00:00.000Z"}


# ── The seam: the scorer's reading of an expected answer ──


def test_two_files_of_one_stix_ground_truth_share_a_reference_key():
    """Identifiers, timestamps and object order differ; the canonical objects and
    triples are the same, so the two datasets' entries pair."""
    actor, malware = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    first = _bundle(actor, malware, _rel(actor, "uses", malware))
    actor2, malware2 = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    second = _bundle(_rel(actor2, "uses", malware2), malware2, actor2)
    other = _bundle(actor, malware)
    assert reference_key("STIX_BUNDLE", first) == reference_key("STIX_BUNDLE", second)
    assert reference_key("STIX_BUNDLE", first) != reference_key("STIX_BUNDLE", other)
    assert reference_key("STIX_BUNDLE", "not a bundle") is None


def test_other_families_pair_on_the_json_value_or_the_trimmed_text():
    assert reference_key("CLASSIFICATION", '["a", "b"]') == reference_key("CLASSIFICATION", '[ "a","b" ]')
    assert reference_key("PROSE", " Paris\n") == reference_key("PROSE", "Paris")
    assert reference_key("PROSE", "Paris") != reference_key("PROSE", "Lyon")


def test_the_canonical_view_keeps_scored_objects_and_relationship_triples():
    actor, malware = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    view = canonical_stix_bundle(_bundle(actor, malware, _rel(actor, "uses", malware)), reference=True)
    assert view is not None
    assert sorted(o["type"] for o in view.objects) == ["malware", "threat-actor"]
    assert [t[1] for t in view.triples] == ["uses"]
    assert canonical_stix_bundle("I could not extract anything") is None


# ── The bundle review ──


def test_the_review_pairs_a_near_miss_and_keeps_a_spurious_object_apart():
    actor, malware = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    expected = _bundle(actor, malware, _rel(actor, "uses", malware))
    # Another name for the actor (an intrusion set, same family of types), the malware
    # found, one object nobody expected, and the relationship on the near-miss actor.
    near, found, extra = _sdo("intrusion-set", "Memento Labs"), _sdo("malware", "Dante"), _sdo("identity", "Kaspersky", identity_class="organization")
    answer = _bundle(near, found, extra, _rel(near, "uses", found))
    review = build_review(expected, [("a", answer), ("b", "no bundle here")])
    assert review is not None
    assert review["unreadable"] == ["b"]
    names = [o["n"] for o in review["objects"]]
    group = dict(zip(names, review["groups"], strict=False))
    # The two actors share the expected actor's group; the identity has its own.
    actor_groups = {review["groups"][i] for i, o in enumerate(review["objects"]) if o["n"] == "Memento Labs"}
    assert len(actor_groups) == 1
    assert group["Kaspersky"] not in actor_groups
    assert len(review["sources"][EXPECTED]) == 2 and len(review["sources"]["a"]) == 3
    # One relationship row: the expected triple, and the answer's on the paired actor.
    assert len(review["relationships"]) == 1
    cells = review["relationships"][0][2]
    assert set(cells) == {EXPECTED, "a"}
    assert review["objects"][cells["a"][1]]["t"] == "intrusion-set"


def test_an_unreadable_expected_bundle_has_no_review():
    assert build_review("not json", [("a", "{}")]) is None


# ── GET /evaluations/compare ──


async def _evaluation(session, name: str, family: str, answers: list[str], agent_name: str) -> tuple[Evaluation, Agent, list[DatasetEntry]]:
    agent = Agent(name=agent_name, builtin_tool_ids=["web"], max_iterations=4)
    evaluation = Evaluation(name=name, family=family, sub_score_weights={"response_accuracy": 1.0})
    dataset = Dataset(name=f"{name} data", family=family)
    session.add_all([agent, evaluation, dataset])
    await session.flush()
    entries = [DatasetEntry(dataset_id=dataset.id, position=i, expected_answer=a, description=f"{name} #{i}") for i, a in enumerate(answers)]
    session.add_all([*entries, EvaluationDataset(evaluation_id=evaluation.id, dataset_id=dataset.id, position=0),
                     EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id)])
    await session.flush()
    return evaluation, agent, entries


async def _run(session, evaluation, agent, model, entries, counts, *, age_hours=0, status="completed", cost=None):
    run = EvaluationRun(evaluation_id=evaluation.id, agent_id=agent.id, model_id=model, trigger_reason="manual",
                        status=status, metric_suite_version="15.0.0", created_at=NOW - timedelta(hours=age_hours))
    session.add(run)
    await session.flush()
    for entry, (num, den) in zip(entries, counts, strict=True):
        result = EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output="[]", score=50.0, duration_seconds=30.0)
        session.add(result)
        await session.flush()
        session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key=MICRO, value=100 * num / den, numerator=num, denominator=den))
        if cost is not None:
            session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key="performance.cost.llm", value=cost))
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_evaluations_compare_on_the_entries_they_share():
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, agent_a, entries_a = await _evaluation(session, "A", "CLASSIFICATION", ['["x"]', '["y"]'], "Harvester")
            b, agent_b, entries_b = await _evaluation(session, "B", "CLASSIFICATION", ['[ "y" ]', '["z"]'], "Grounded")
            old = await _run(session, a, agent_a, "m1", entries_a, [(1, 2), (0, 2)], age_hours=5)
            new = await _run(session, a, agent_a, "m1", entries_a, [(2, 2), (2, 2)], age_hours=1, cost=0.5)
            await _run(session, a, agent_a, "m1", entries_a, [(0, 2), (0, 2)], status="failed")
            run_b = await _run(session, b, agent_b, "m1", entries_b, [(1, 2), (2, 4)])
            await session.commit()

            served = EvaluationComparisonResponse.model_validate(
                await compare_evaluations_route(ids=f"{a.id},{b.id}", user=admin, db=session)
            )
            picked = EvaluationComparisonResponse.model_validate(
                await compare_evaluations_route(ids=f"{a.id},{b.id}", run_ids=str(old.id), user=admin, db=session)
            )
    finally:
        await cleanup()

    assert served.family == "CLASSIFICATION"
    assert MICRO in [m.key for m in served.metrics]
    # The newest completed run per subject and model; the failed one is never offered.
    assert [r.id for r in served.evaluations[0].runs] == [str(new.id)]
    assert [r.id for r in served.evaluations[0].run_choices] == [str(new.id), str(old.id)]
    assert [r.id for r in served.evaluations[1].runs] == [str(run_b.id)]
    assert served.evaluations[0].subjects[0].name == "Harvester"
    assert served.evaluations[0].datasets[0].entry_count == 2
    # ["y"] is in both datasets (written differently): one shared entry, two of one side.
    shared = [e for e in served.entries if len(e.members) == 2]
    assert len(served.entries) == 3 and len(shared) == 1
    assert {m.evaluation_id for m in shared[0].members} == {str(a.id), str(b.id)}
    cells = {(c.run_id, c.entry_key): c for c in served.cells}
    assert cells[(str(new.id), shared[0].key)].metrics[MICRO] == [2.0, 2.0]
    assert cells[(str(new.id), shared[0].key)].cost == 0.5
    assert cells[(str(run_b.id), shared[0].key)].metrics[MICRO] == [1.0, 2.0]
    # A run the caller names replaces its evaluation's default; the other keeps its own.
    assert [r.id for r in picked.evaluations[0].runs] == [str(old.id)]
    assert [r.id for r in picked.evaluations[1].runs] == [str(run_b.id)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evaluations_of_two_families_or_none_are_refused():
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, _, _ = await _evaluation(session, "A", "CLASSIFICATION", ['["x"]'], "One")
            b, _, _ = await _evaluation(session, "B", "PROSE", ["x"], "Two")
            await session.commit()
            with pytest.raises(HTTPException) as families:
                await compare_evaluations_route(ids=f"{a.id},{b.id}", user=admin, db=session)
            with pytest.raises(HTTPException) as none:
                await compare_evaluations_route(ids="", user=admin, db=session)
            with pytest.raises(HTTPException) as missing:
                await compare_evaluations_route(ids=f"{a.id},{uuid.uuid4()}", user=admin, db=session)
    finally:
        await cleanup()
    assert families.value.status_code == 422 and "one family" in families.value.detail
    assert none.value.status_code == 422
    assert missing.value.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_evaluation_compares_its_models_on_all_its_entries():
    """One evaluation alone (the same id twice reads as one): its newest run per model, every
    entry its own and shared by the runs."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, agent, entries = await _evaluation(session, "A", "CLASSIFICATION", ['["x"]', '["y"]'], "Grounded")
            await _run(session, a, agent, "m1", entries, [(0, 2), (0, 2)], age_hours=5)
            m1 = await _run(session, a, agent, "m1", entries, [(1, 2), (2, 2)], age_hours=1)
            m2 = await _run(session, a, agent, "m2", entries, [(2, 2), (1, 2)], age_hours=2)
            await session.commit()
            served = EvaluationComparisonResponse.model_validate(
                await compare_evaluations_route(ids=f"{a.id},{a.id}", user=admin, db=session)
            )
    finally:
        await cleanup()

    assert [e.id for e in served.evaluations] == [str(a.id)]
    assert [r.id for r in served.evaluations[0].runs] == [str(m1.id), str(m2.id)]
    # Its trigger groups runs into launches on the Leaderboard, as Activity groups them.
    assert {r.trigger_reason for r in served.evaluations[0].run_choices} == {"manual"}
    assert len(served.entries) == 2 and all(len(e.members) == 1 for e in served.entries)
    cells = {(c.run_id, c.entry_key): c.metrics[MICRO] for c in served.cells}
    assert sorted(v for (rid, _), v in cells.items() if rid == str(m2.id)) == [[1.0, 2.0], [2.0, 2.0]]


# ── GET /evaluations/bundle-review ──


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_evaluations_answers_on_one_stix_entry_are_reviewed_together():
    actor, malware = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    expected = _bundle(actor, malware, _rel(actor, "uses", malware))
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, agent_a, [entry_a] = await _evaluation(session, "A", "STIX_BUNDLE", [expected], "Harvester")
            b, agent_b, [entry_b] = await _evaluation(session, "B", "STIX_BUNDLE", [_bundle(malware, actor, _rel(actor, "uses", malware))], "Grounded")
            entry_a.input_prompt = "Memento Labs uses Dante.\n\n\nPage two."
            results = []
            # B's run has its judge off: its stored 0-or-60 score is no verdict.
            for evaluation, agent, entry, answer, scorers in (
                (a, agent_a, entry_a, _bundle(actor), None),
                (b, agent_b, entry_b, expected, ["deterministic"]),
            ):
                run = EvaluationRun(evaluation_id=evaluation.id, agent_id=agent.id, model_id="m1", trigger_reason="manual",
                                    status="completed", scorer_selection=scorers)
                session.add(run)
                await session.flush()
                result = EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output=answer, score=60.0)
                session.add(result)
                await session.flush()
                session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key="stix.entity_prf", value=50.0, numerator=1, denominator=2))
                results.append(result)
            other, agent_o, [entry_o] = await _evaluation(session, "C", "STIX_BUNDLE", [_bundle(actor)], "Third")
            run_o = EvaluationRun(evaluation_id=other.id, agent_id=agent_o.id, model_id="m1", trigger_reason="manual", status="completed")
            session.add(run_o)
            await session.flush()
            off = EvaluationResult(run_id=run_o.id, entry_id=entry_o.id, agent_output="{}", score=0.0)
            session.add(off)
            await session.commit()

            served = BundleReviewResponse.model_validate(
                await get_bundle_review(result_ids=",".join(str(r.id) for r in results), user=admin, db=session)
            )
            with pytest.raises(HTTPException) as mismatch:
                await get_bundle_review(result_ids=f"{results[0].id},{off.id}", user=admin, db=session)
            with pytest.raises(HTTPException) as unknown:
                await get_bundle_review(result_ids=str(uuid.uuid4()), user=admin, db=session)
    finally:
        await cleanup()

    assert [a.evaluation_name for a in served.answers] == ["A", "B"]
    assert [a.subject_name for a in served.answers] == ["Harvester", "Grounded"]
    assert [a.judge_score for a in served.answers] == [60.0, None]
    assert served.answers[0].metrics == {"stix.entity_prf": 50.0}
    assert served.entry.text_source == "prompt" and served.entry.pages == ["Memento Labs uses Dante.\n\n\nPage two."]
    assert len(served.sources[EXPECTED]) == 2
    assert len(served.sources[str(results[0].id)]) == 1 and len(served.sources[str(results[1].id)]) == 2
    assert mismatch.value.status_code == 422 and "same expected bundle" in mismatch.value.detail
    assert unknown.value.status_code == 404


# ── What both pages read from one stored answer ──


def _scored(value: float, **detail) -> dict:
    return {"status": "scored", "value": value, "detail": detail}


def test_an_answer_gives_its_counts_and_whether_the_scorer_could_read_it():
    readable = {"stix.entity_prf": _scored(34.1, tp=7, fp=17, fn=10, numerator=14, denominator=41),
                "performance.cost.llm": _scored(0.13)}
    unreadable = {"stix.entity_prf": _scored(0.0, reason="prediction_not_well_formed", numerator=0, denominator=34)}
    assert metric_counts(readable, ("stix.entity_prf",)) == {"stix.entity_prf": [7.0, 17.0, 10.0]}
    assert metric_counts(unreadable, ("stix.entity_prf",)) == {}
    assert not is_unreadable(readable) and is_unreadable(unreadable)
    assert is_unreadable(None) is False


def test_a_judge_score_exists_only_when_the_judge_gave_a_verdict():
    assert judge_score(60.0, scorer_selection=None, is_judge_failure=False, anomaly=None) == 60.0
    assert judge_score(0.0, scorer_selection=[], is_judge_failure=False, anomaly=None) is None
    assert judge_score(60.0, scorer_selection=["judge"], is_judge_failure=True, anomaly=None) is None
    assert judge_score(60.0, scorer_selection=None, is_judge_failure=False, anomaly="transient_error") is None


def test_reruns_of_one_setup_share_a_setup_key_whatever_the_model():
    agent = uuid.uuid4()
    first = EvaluationRun(agent_id=agent, model_id="m1", persona_version_number=3, execution_mode="full_loop", skill_versions={"s": 2})
    rerun = EvaluationRun(agent_id=agent, model_id="m2", persona_version_number=3, execution_mode="full_loop", skill_versions={"s": 2})
    changed = EvaluationRun(agent_id=agent, model_id="m1", persona_version_number=4, execution_mode="full_loop", skill_versions={"s": 2})
    assert setup_of(first)[0] == setup_of(rerun)[0] != setup_of(changed)[0]
    assert setup_of(first)[1]["persona_version"] == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_runs_cells_carry_counts_unreadable_answers_and_judge_scores():
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, agent, entries = await _evaluation(session, "A", "CLASSIFICATION", ['["x"]', '["y"]'], "Harvester")
            run = EvaluationRun(evaluation_id=a.id, agent_id=agent.id, model_id="m1", trigger_reason="schedule", status="completed",
                                score=72.5, persona_version_number=2, created_at=NOW)
            session.add(run)
            await session.flush()
            details = [
                {"metric_results": {MICRO: _scored(50.0, tp=1, fp=1, fn=1, numerator=2, denominator=4)}},
                {"metric_results": {MICRO: _scored(0.0, reason="prediction_not_scorable", numerator=0, denominator=2)}},
            ]
            for entry, detail, (num, den) in zip(entries, details, [(2, 4), (0, 2)], strict=True):
                result = EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output="x", score=40.0, execution_details=detail)
                session.add(result)
                await session.flush()
                session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key=MICRO, value=100 * num / den, numerator=num, denominator=den))
            await session.commit()
            served = EvaluationComparisonResponse.model_validate(await compare_evaluations_route(ids=str(a.id), user=admin, db=session))
    finally:
        await cleanup()

    cells = sorted(served.cells, key=lambda c: c.unreadable)
    assert cells[0].counts == {MICRO: [1.0, 1.0, 1.0]} and not cells[0].unreadable
    assert cells[1].unreadable and cells[1].counts == {}
    assert [c.judge_score for c in cells] == [40.0, 40.0]
    run_view = served.evaluations[0].runs[0]
    assert run_view.judge_score == 72.5 and run_view.trigger_reason == "schedule"
    assert run_view.setup_key and run_view.setup["persona_version"] == 2
    # The suite scoring now, against which a run's own version tells a drift.
    assert served.metric_suite_version
    assert run_view.rescore_error is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_runs_entries_are_reviewed_in_one_batch_without_their_text():
    actor, malware = _sdo("threat-actor", "Memento Labs"), _sdo("malware", "Dante")
    first, second = _bundle(actor, malware, _rel(actor, "uses", malware)), _bundle(actor)
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _admin(session)
            a, agent, entries = await _evaluation(session, "A", "STIX_BUNDLE", [first, second], "Grounded")
            for entry in entries:
                entry.input_prompt = "Memento Labs uses Dante."
            run = EvaluationRun(evaluation_id=a.id, agent_id=agent.id, model_id="m1", trigger_reason="manual", status="completed")
            session.add(run)
            await session.flush()
            results = []
            for entry, answer in zip(entries, [_bundle(actor), "I could not do it"], strict=True):
                result = EvaluationResult(run_id=run.id, entry_id=entry.id, agent_output=answer, score=55.0,
                                          evaluator_reasoning="Missing the malware.", duration_seconds=12.0,
                                          execution_details={"metric_results": {"stix.entity_prf": _scored(66.7, tp=1, fp=0, fn=1, numerator=2, denominator=3)}})
                session.add(result)
                await session.flush()
                session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key="performance.cost.llm", value=0.25))
                results.append(result)
            await session.commit()
            served = [
                BundleReviewResponse.model_validate(r)
                for r in await get_bundle_reviews(BundleReviewsRequest(groups=[[str(results[0].id)], [str(results[1].id)]]), user=admin, db=session)
            ]
            with_text = BundleReviewResponse.model_validate(await get_bundle_review(result_ids=str(results[0].id), user=admin, db=session))
    finally:
        await cleanup()

    assert len(served) == 2
    assert served[0].entry.pages == [] and served[0].answers[0].judge_reasoning is None
    assert with_text.entry.pages == ["Memento Labs uses Dante."] and with_text.answers[0].judge_reasoning == "Missing the malware."
    answer = with_text.answers[0]
    assert answer.counts == {"stix.entity_prf": [1.0, 0.0, 1.0]} and answer.cost == 0.25 and answer.seconds == 12.0
    assert served[1].unreadable == [str(results[1].id)]
    assert (with_text.entry.position, with_text.entry.entry_count, with_text.entry.dataset_name) == (0, 2, "A data")
    assert with_text.entry.key and served[0].entry.key == with_text.entry.key
    assert with_text.metric_suite_version and with_text.answers[0].metric_suite_version is None
