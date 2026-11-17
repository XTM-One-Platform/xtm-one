"""Evaluation export and import, and the run settings, against a real (throwaway) PostgreSQL database.

The package round trip keeps each result's classification, provenance and metric rows,
and older or malformed packages still import. The run settings (mode, prompt guard,
tool approval) are recorded on each run, kept by a copy and carried by the package. The
resume secret never leaves the server.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    CreateEvaluationRequest,
    UpdateEvaluationRequest,
)
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.user import User
from tests.evaluation_database import (
    _admin,
    _make_db,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)
from tests.evaluation_helpers import (
    HeadlinesFake,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── the export carries each result's classification ──────────────────
#
# ``_compute_run_headlines`` excludes judge failures and execution anomalies.
# A score exported without its flags comes back from an import as an ordinary
# scored result, and the next recompute averages it in.


_ROUND_TRIP_CHAIN = {
    "calls": [{"kind": "model", "name": "llm_call", "model": "m", "duration_ms": 5.0, "outcome": "success", "usage": {"input": 10, "output": 2, "total": 12}}],
    "usage_by_model": [{"model": "m", "usage": {"input": 10, "output": 2, "total": 12}}],
    "retries": 0,
    "complete": True,
}


# An expected answer a STIX dataset accepts on import.
_BUNDLE = '{"type": "bundle", "id": "bundle--1", "objects": []}'


async def _export_fixture(sm):
    """One evaluation, one run, three results: valid, judge-failed, anomalous."""
    async with sm() as s:
        admin = (
            await s.execute(select(User).where(User.email == "a@x.io"))
        ).scalar_one_or_none() or await _admin(s)
        evaluation = Evaluation(
            name=f"E-{uuid.uuid4().hex[:8]}",
            family="STIX_BUNDLE",
            apply_prompt_guards=True,
            require_tool_approval=True,
        )
        s.add(evaluation)
        await s.flush()
        agent = Agent(user_id=admin.id, name=f"A-{uuid.uuid4().hex[:8]}")
        ds = Dataset(name=f"d-{uuid.uuid4().hex[:8]}", family="STIX_BUNDLE")
        s.add_all([agent, ds])
        await s.flush()
        s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
        s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=ds.id))
        entries = [
            DatasetEntry(dataset_id=ds.id, position=i, expected_answer=_BUNDLE)
            for i in range(3)
        ]
        s.add_all(entries)
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="m",
            status="completed",
            trigger_reason="manual",
            total_entries=3,
            execution_mode="full_loop",
            apply_prompt_guards=True,
            require_tool_approval=True,
        )
        s.add(run)
        await s.flush()
        # The shape ``extract_metrics`` reads: two durations plus the package's
        # own blob. The skipped entry makes the round trip keep the scored-only
        # rule rather than copy the blob wholesale.
        details = {
            "judge_duration_ms": 2000,
            "total_duration_ms": 9000,
            "metric_results": {
                "stix.envelope_validity": {"status": "scored", "value": 90.0},
                "stix.unavailable_one": {"status": "skipped", "value": None},
            },
            # The chain ledger the performance metrics were scored from.
            "chain": _ROUND_TRIP_CHAIN,
        }
        s.add_all([
            EvaluationResult(
                run_id=run.id, entry_id=entries[0].id, agent_output="ok",
                score=90, sub_scores={}, execution_details=dict(details),
                # REUSED: this run did not execute the prediction, so Run Duration
                # is the judge time alone. Lost in the round trip, the
                # imported row would claim agent work.
                is_reused=True,
            ),
            EvaluationResult(
                run_id=run.id, entry_id=entries[1].id, agent_output="junk",
                score=0, sub_scores={}, is_judge_failure=True,
                execution_details=dict(details),
            ),
            EvaluationResult(
                run_id=run.id, entry_id=entries[2].id, agent_output="Error: 503",
                score=0, sub_scores={},
                execution_anomaly_kind="transient_error",
                execution_details=dict(details),
            ),
        ])
        await s.commit()
        return evaluation.id, admin.id


def _asgi_request():
    """A real Request: the import path reads several ASGI attributes for audit.

    A genuine scope gives the whole surface at once and cannot drift from what
    Starlette exposes.
    """
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/evaluations/import",
            "headers": [],
            "client": ("test", 1),
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )


async def _round_trip(
    sm, monkeypatch, *, strip_classification=False, strip_provenance=False,
    corrupt_details=False, lie_about_reuse=False, oversized_metric_key=False,
    non_finite_details=False, strip_prompt_guards=False,
):
    """Export the fixture evaluation and import it back, returning the new rows."""
    import app.api.v1.agents as agents_api
    from app.api.v1.evaluations import (
        _streaming_response_to_bytes,
        export_evaluation,
        import_evaluation,
    )

    async def _stub_export_agent(*_a, **_k):
        # Shaped for ``ImportAgentRequest`` so the import half accepts it; a real
        # agent export would need a Request unrelated to the RESULT rows.
        return SimpleNamespace(
            body=json.dumps(
                {"version": 3, "export_type": "agent", "agent": {"name": "stub"}}
            ).encode()
        )

    monkeypatch.setattr(agents_api, "export_agent", _stub_export_agent)

    evaluation_id, admin_id = await _export_fixture(sm)
    async with sm() as s:
        actor = await s.get(User, admin_id)
        response = await export_evaluation(
            evaluation_id=str(evaluation_id), request=_asgi_request(), user=actor, db=s
        )
        zip_bytes = await _streaming_response_to_bytes(response)

    if non_finite_details:
        # ``json.loads`` accepts NaN/Infinity, so a package can carry them, and
        # ``execution_details`` is stored verbatim in a Postgres JSON column, whose
        # serializer emits them as bare literals that are not valid JSON.
        src_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src_zip.namelist():
                data = src_zip.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    for result in payload.get("results", []):
                        details = result.get("execution_details") or {}
                        details["judge_duration_ms"] = float("nan")
                        details.setdefault("metric_results", {})["pkg.bad"] = {
                            "status": "scored",
                            "value": float("inf"),
                        }
                        result["execution_details"] = details
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if oversized_metric_key:
        # A metric key the column cannot hold: ``EvaluationMetric.key`` is
        # ``String(100)``. Postgres rejects the row on flush and aborts the
        # transaction, so without a savepoint the audit and commit fail and the
        # whole import with them.
        src_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src_zip.namelist():
                data = src_zip.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    # ONE result only: the others must keep their metrics, which
                    # corrupting all of them could not show.
                    for result in payload.get("results", []):
                        if result.get("agent_output") != "junk":
                            continue
                        details = result.get("execution_details") or {}
                        details.setdefault("metric_results", {})[
                            "pkg." + "x" * 200
                        ] = {"status": "scored", "value": 1.0}
                        result["execution_details"] = details
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if lie_about_reuse:
        # ``"false"`` where a JSON boolean belongs. ``bool("false")`` is True, so
        # a coercing read would mark the result reused and then measure its Run
        # Duration as the judge time alone.
        src_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src_zip.namelist():
                data = src_zip.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    for result in payload.get("results", []):
                        result["is_reused"] = "false"
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if corrupt_details:
        # A package whose ``execution_details`` is not the shape the engine
        # writes. The import derives metric rows from it, and a bad one
        # costs that result its metrics, not the import.
        src_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src_zip.namelist():
                data = src_zip.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    for result in payload.get("results", []):
                        result["execution_details"] = (
                            "not a mapping at all"
                            if result.get("agent_output") == "ok"
                            else {"metric_results": "not a mapping either"}
                        )
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if strip_provenance:
        # An older exported package: no provenance key at all, not a false
        # one. Removed from the real package, as below.
        src_zip = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src_zip.namelist():
                data = src_zip.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    for result in payload.get("results", []):
                        result.pop("is_reused", None)
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if strip_classification:
        # An older exported package: the fields are absent, not false.
        # Built by removing them, so the older shape differs in nothing else.
        src = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src.namelist():
                data = src.read(name)
                if name.startswith("evaluation_runs/"):
                    payload = json.loads(data)
                    for result in payload.get("results", []):
                        for key in (
                            "is_judge_failure",
                            "execution_anomaly_kind",
                        ):
                            result.pop(key, None)
                    data = json.dumps(payload).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    if strip_prompt_guards:
        # An older exported package: neither the evaluation nor its runs
        # name the setting.
        src = zipfile.ZipFile(io.BytesIO(zip_bytes))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for name in src.namelist():
                data = src.read(name)
                if name == "evaluation.json":
                    definition = json.loads(data)
                    # Nor the tool approval setting.
                    for key in ("apply_prompt_guards", "require_tool_approval"):
                        definition["evaluation"].pop(key, None)
                        for run_bundle in definition.get("evaluation_runs", []):
                            run_bundle.pop(key, None)
                    data = json.dumps(definition).encode()
                out.writestr(name, data)
        zip_bytes = buf.getvalue()

    async with sm() as s:
        actor = await s.get(User, admin_id)
        await import_evaluation(
            request=_asgi_request(),
            file=UploadFile(filename="e.zip", file=io.BytesIO(zip_bytes)),
            user=actor,
            db=s,
            _ai_gate=None,
        )
        await s.commit()

    async with sm() as s:
        rows = (
            await s.execute(
                select(EvaluationResult).where(
                    EvaluationResult.run_id.in_(
                        select(EvaluationRun.id).where(
                            EvaluationRun.evaluation_id != evaluation_id
                        )
                    )
                )
            )
        ).scalars().all()
    return {r.agent_output: r for r in rows}


async def test_an_imported_evaluation_arrives_with_automatic_runs_paused(monkeypatch):
    """Its schedule is the source instance's: it never fires here until an admin turns it on."""
    sm, cleanup = await _make_db()
    try:
        imported_results = await _round_trip(sm, monkeypatch)
        async with sm() as s:
            # The imported evaluation is the one its imported runs belong to.
            imported_run = await s.get(EvaluationRun, next(iter(imported_results.values())).run_id)
            enabled = dict((await s.execute(select(Evaluation.id, Evaluation.enabled))).all())
        assert enabled.pop(imported_run.evaluation_id) is False
        assert list(enabled.values()) == [True]
    finally:
        await cleanup()


async def test_a_round_trip_keeps_what_each_run_and_result_recorded(monkeypatch):
    """An export then import keeps each result's classification, chain ledger and
    provenance, and each run's mode and settings.

    Classification: the copy excludes what the source excluded, checked
    per result, since a bundle writing one value for every flag would keep the
    keys and lose exactly the results the headline excludes. The chain ledger:
    a reuse or rescore of the imported result reads the chain it was
    scored from. Provenance: an imported result never claims work it did
    not do. The mode and the prompt guard and tool approval
    settings: the copy runs as the source ran.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch)
        assert imported, "the round trip imported no results at all"

        assert imported["junk"].is_judge_failure is True, (
            "the judge failure came back as a valid result, so the first "
            "headline recompute over the imported evaluation averages it in"
        )
        assert imported["junk"].execution_anomaly_kind is None
        assert imported["Error: 503"].execution_anomaly_kind == "transient_error"
        assert imported["ok"].is_judge_failure is False
        assert imported["ok"].execution_anomaly_kind is None

        for result in imported.values():
            assert result.execution_details["chain"] == _ROUND_TRIP_CHAIN

        assert imported["ok"].is_reused is True, (
            "the reused prediction came back claiming a fresh execution, which "
            "is the claim the package had no basis for"
        )
        # Per result: a bundle writing True for everything would pass the check
        # above and invent reuse for results that did execute.
        assert imported["junk"].is_reused is False
        assert imported["Error: 503"].is_reused is False
        # DELIBERATELY absent: the payload has no result id to point at, and
        # NULL states "reused, source not recorded here".
        assert imported["ok"].reused_from_result_id is None

        async with sm() as s:
            run = await s.get(EvaluationRun, imported["ok"].run_id)
            evaluation = await s.get(Evaluation, run.evaluation_id)
        assert run.execution_mode == "full_loop"
        assert (evaluation.apply_prompt_guards, run.apply_prompt_guards) == (True, True)
        assert (evaluation.require_tool_approval, run.require_tool_approval) == (True, True)
    finally:
        await cleanup()


async def test_a_package_written_before_the_fields_still_imports(monkeypatch):
    """A package without the classification fields still imports, every result
    valid.

    An import reproduces what was exported; it does not re-derive the flags from
    ``agent_output`` and ``execution_mode``.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, strip_classification=True)
        assert imported, "an older package failed to import at all"
        assert all(not r.is_judge_failure for r in imported.values())
        assert all(r.execution_anomaly_kind is None for r in imported.values())
    finally:
        await cleanup()


# ── every write path classifies its results ──────────────────────────
#
# ``_compute_run_headlines`` excludes judge failures from the headline. The
# replay endpoint and the callback resume persist ``is_judge_failure`` too, or
# a failed judge is averaged as a real score by the pass they trigger.


async def _replay_fixture(sm, *, prior_failure=False, execution_details=None):
    """One completed run with one scored result, ready to be re-judged."""
    async with sm() as s:
        admin = (
            await s.execute(select(User).where(User.email == "a@x.io"))
        ).scalar_one_or_none() or await _admin(s)
        evaluation = Evaluation(name=f"E-{uuid.uuid4().hex[:8]}", family="STIX_BUNDLE")
        s.add(evaluation)
        await s.flush()
        agent = Agent(user_id=admin.id, name=f"A-{uuid.uuid4().hex[:8]}")
        ds = Dataset(name=f"d-{uuid.uuid4().hex[:8]}", family="STIX_BUNDLE")
        s.add_all([agent, ds])
        await s.flush()
        entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer=_BUNDLE)
        s.add(entry)
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="m",
            status="completed",
            trigger_reason="manual",
            total_entries=1,
        )
        s.add(run)
        await s.flush()
        eval_result = EvaluationResult(
            run_id=run.id,
            entry_id=entry.id,
            agent_output="out",
            score=80,
            sub_scores={},
            is_judge_failure=prior_failure,
            execution_details=execution_details,
        )
        s.add(eval_result)
        await s.commit()
        return run.id, eval_result.id, admin.id


@pytest.mark.parametrize(
    ("prior_failure", "verdict", "excluded"),
    [
        (False, True, True),
        (True, False, False),
        (False, None, True),
        (False, "absent", True),
    ],
    ids=[
        "newly_failing_replay_is_excluded",
        "newly_passing_replay_is_included",
        "explicit_none_is_excluded",
        "missing_key_is_excluded",
    ],
)
async def test_a_replay_persists_the_judges_verdict_on_itself(
    monkeypatch, prior_failure, verdict, excluded
):
    """A replay persists the judge's verdict on the result it re-judges.

    The endpoint recomputes the headline that reads this flag. Both directions
    run: a replay that newly FAILS is excluded, one that newly SUCCEEDS counts.
    Without an explicit ``False`` the result is excluded: ``score_result`` always
    carries the key, so this pins the direction the code fails if that contract
    breaks. Excluding is visible, while including would silently count a verdict
    nobody said was sound.
    """
    from app.api.v1.evaluations import replay_judge_result

    sm, cleanup = await _make_db()
    try:
        run_id, result_id, admin_id = await _replay_fixture(
            sm, prior_failure=prior_failure
        )

        async def _judge(*_a, **_k):
            data = {
                "score": 10 if verdict is True else 90,
                "sub_scores": {},
                "evaluator_reasoning": "r",
            }
            if verdict != "absent":
                data["is_judge_failure"] = verdict
            return data

        # Imported inside the endpoint, so patched at the source module.
        import app.services.evaluation_engine as ee

        monkeypatch.setattr(ee, "score_result", _judge)
        monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())

        async with sm() as s:
            actor = await s.get(User, admin_id)
            await replay_judge_result(
                run_id=str(run_id),
                result_id=str(result_id),
                body=SimpleNamespace(judge_max_tokens=None),
                user=actor,
                db=s,
            )

        async with sm() as s:
            row = await s.get(EvaluationResult, result_id)

        assert row.is_judge_failure is excluded, (
            f"the replay left is_judge_failure={row.is_judge_failure} for a judge "
            f"verdict of {verdict!r}. The headline this endpoint recomputes "
            "excludes judge failures, so it now disagrees with the replay that "
            "triggered it."
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("execution_details", "expected"),
    [
        pytest.param(
            {"execution_mode": "full_loop", "available_tools": ["search"], "traces": []},
            ["search"],
            id="stored_list",
        ),
        # Stored by an older build: unknown, never the tools the entry called.
        pytest.param(
            {"execution_mode": "full_loop", "traces": [{"type": "tool", "tool_name": "fetch"}]},
            None,
            id="no_stored_list",
        ),
    ],
)
async def test_a_replay_judges_against_the_tools_stored_with_the_result(
    monkeypatch, execution_details, expected
):
    """A replay gives the judge the tool list stored with the result."""
    from app.api.v1.evaluations import replay_judge_result

    sm, cleanup = await _make_db()
    try:
        run_id, result_id, admin_id = await _replay_fixture(
            sm, execution_details=execution_details
        )
        seen: dict = {}

        async def _judge(*_a, **kwargs):
            seen["available_tools"] = kwargs.get("available_tools")
            return {"score": 90, "sub_scores": {}, "evaluator_reasoning": "r", "is_judge_failure": False}

        import app.services.evaluation_engine as ee

        monkeypatch.setattr(ee, "score_result", _judge)
        monkeypatch.setattr(ee, "_compute_run_headlines", HeadlinesFake())

        async with sm() as s:
            actor = await s.get(User, admin_id)
            await replay_judge_result(
                run_id=str(run_id),
                result_id=str(result_id),
                body=SimpleNamespace(judge_max_tokens=None),
                user=actor,
                db=s,
            )

        assert seen["available_tools"] == expected
    finally:
        await cleanup()


# ── the package carries provenance and metric rows ───────────────────
#
# Provenance (``is_reused``), or every imported result claims a prediction it did
# not execute; and the metric rows, or the metrics panel and
# ``get_run_aggregates`` read blank for an imported evaluation.
#
# They go together: ``extract_metrics`` reads the provenance to decide what Run
# Duration MEASURES, and without it writes a duration claiming agent work that
# never happened (as the flags prevent for scores).


async def _metric_rows(sm, result_id):
    """The persisted metrics for one result, as ``{key: value}``."""
    async with sm() as s:
        rows = (
            await s.execute(
                select(EvaluationMetric.key, EvaluationMetric.value).where(
                    EvaluationMetric.result_id == result_id
                )
            )
        ).all()
    return {key: value for key, value in rows}


async def test_an_imported_evaluation_has_metric_rows(monkeypatch):
    """An imported evaluation has its metric rows."""
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch)
        rows = await _metric_rows(sm, imported["junk"].id)

        assert rows, (
            "the import wrote no metric rows, so the metrics panel and "
            "get_run_aggregates both read a blank for this evaluation"
        )
        # Reproduced from the package, not re-derived.
        assert rows["stix.envelope_validity"] == 90.0
        assert rows["judge_seconds"] == 2.0
        # The scored-only rule survives: a skipped outcome contributes no number.
        assert "stix.unavailable_one" not in rows
    finally:
        await cleanup()


async def test_a_reused_results_run_duration_is_the_judge_time(monkeypatch):
    """An imported reused result's Run Duration is the judge time alone.

    ``run_duration_seconds`` is the judge time for a reused result and the total
    for a fresh one, so the provenance must reach ``extract_metrics``,
    not just the row.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch)

        reused = await _metric_rows(sm, imported["ok"].id)
        fresh = await _metric_rows(sm, imported["junk"].id)

        assert reused["run_duration_seconds"] == 2.0, (
            "the reused result's Run Duration came back as the total elapsed "
            "time, claiming agent work no run performed"
        )
        assert fresh["run_duration_seconds"] == 9.0, (
            "a freshly executed result must still measure the whole execution"
        )
    finally:
        await cleanup()


async def test_a_package_written_before_provenance_still_imports(monkeypatch):
    """A package without the provenance key still imports, its results fresh."""
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, strip_provenance=True)
        assert imported, "a package without provenance failed to import at all"

        # Absent means "fresh", what such a package always claimed.
        assert all(not r.is_reused for r in imported.values())

        # Its metrics are still written: the rows depend on the key's value when
        # present, not on its presence.
        rows = await _metric_rows(sm, imported["ok"].id)
        assert rows["stix.envelope_validity"] == 90.0
        assert rows["run_duration_seconds"] == 9.0
    finally:
        await cleanup()


async def test_a_package_with_malformed_execution_details_still_imports(monkeypatch):
    """A malformed ``execution_details`` costs that result its metrics, never the
    import.

    The package is user-uploaded, and nothing requires a shape of the blob the
    metric rows are derived from.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, corrupt_details=True)
        assert imported, "a package with an unreadable blob failed to import"

        # Both corruptions: the blob itself, and its inner mapping.
        assert await _metric_rows(sm, imported["ok"].id) == {}
        assert await _metric_rows(sm, imported["junk"].id) == {}

        # The result rows are unaffected: still reproduced from the package.
        assert imported["junk"].is_judge_failure is True
    finally:
        await cleanup()


async def test_a_metric_failure_does_not_cost_the_import(monkeypatch):
    """A failure deriving metric rows never fails the import.

    As in the engine, the results are already persisted: a number that cannot
    be derived is a warning, not the loss of the package.
    """
    import app.api.v1.evaluations as evaluations_api

    async def _explode(*_a, **_k):
        raise RuntimeError("metric backend is down")

    monkeypatch.setattr(evaluations_api, "log_result_metrics", _explode)

    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch)

        assert imported, "a metric-logging failure took the whole import with it"
        # Everything the package carried is still there.
        assert imported["ok"].is_reused is True
        assert imported["junk"].is_judge_failure is True
        # And the rows it could not derive are absent.
        assert await _metric_rows(sm, imported["ok"].id) == {}
    finally:
        await cleanup()


async def test_a_string_where_a_boolean_belongs_is_not_reuse(monkeypatch):
    """Only the JSON literal ``true`` means reused; the string ``"false"`` does not.

    ``bool("false")`` is True, so a coercing read would mark an executed result
    reused and record its Run Duration as the judge time alone: an invented
    measurement.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, lie_about_reuse=True)
        assert imported, "the round trip imported no results at all"

        assert all(not r.is_reused for r in imported.values()), (
            "a string was coerced into a reuse claim the package never made"
        )
        # What the flag decides: the whole execution, not the judge's share.
        rows = await _metric_rows(sm, imported["ok"].id)
        assert rows["run_duration_seconds"] == 9.0
    finally:
        await cleanup()


async def test_a_rejected_metric_row_does_not_roll_back_the_import(monkeypatch):
    """A metric row PostgreSQL rejects does not roll back the import.

    The key guard is disabled so an unstorable key reaches the database, standing
    in for a failure nobody has enumerated. Without the savepoint the aborted
    transaction takes the audit, the commit and the whole package with it.
    """
    import app.services.evaluation.metrics as run_metrics

    monkeypatch.setattr(run_metrics, "_is_storable_key", lambda _key: True)

    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, oversized_metric_key=True)

        assert imported, (
            "a metric row the column could not hold took the whole import with it"
        )
        # The package's substance is intact: flushed before the savepoint.
        assert imported["ok"].is_reused is True
        assert imported["junk"].is_judge_failure is True

        # The cost of the savepoint's scope: the run's derived rows go as a unit,
        # good results' included. The guard disabled above bounds that.
        assert await _metric_rows(sm, imported["ok"].id) == {}
    finally:
        await cleanup()


async def test_an_unstorable_metric_key_costs_only_itself(monkeypatch):
    """An unstorable metric key costs only that metric.

    The key is refused before the write, so there is no database error to
    contain and no subtransaction per result to pay for.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, oversized_metric_key=True)
        assert imported, "an unstorable metric key took the whole import with it"

        # The result that carried it keeps everything else it recorded.
        offending = await _metric_rows(sm, imported["junk"].id)
        assert offending["stix.envelope_validity"] == 90.0
        assert offending["judge_seconds"] == 2.0
        assert not any(len(key) > 100 for key in offending), (
            "a key the column cannot hold reached the table"
        )

        # The other results keep theirs: this fails if the run rolls back as a unit.
        clean = await _metric_rows(sm, imported["ok"].id)
        assert clean["stix.envelope_validity"] == 90.0
        assert clean["run_duration_seconds"] == 2.0
    finally:
        await cleanup()


async def test_a_package_carrying_nan_still_imports(monkeypatch):
    """A package carrying NaN still imports.

    ``extract_metrics`` drops non-finite numbers, but the result row stores
    ``execution_details`` verbatim into a Postgres JSON column first, where
    ``NaN``/``Infinity`` are not valid JSON and the row is rejected.
    """
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, non_finite_details=True)
        assert imported, "a package carrying NaN took the whole import with it"
        # The measurements it could not honour are absent, not invented.
        rows = await _metric_rows(sm, imported["ok"].id)
        assert "judge_seconds" not in rows
        assert "pkg.bad" not in rows
    finally:
        await cleanup()


# --- run mode: a run serves its own, Full Loop by default, an unknown mode refused ---


async def test_a_run_reads_the_mode_it_recorded_and_its_fallbacks_never_the_evaluations():
    """``list_runs`` and ``get_run`` serve a run's own mode and fallbacks, never the
    evaluation's.

    The evaluation says Full Loop. One run recorded LLM Only; one recorded before
    the column reads its mode from its results, one of which fell back. Every
    shape of the resolution itself is pinned in ``test_evaluation_run_stamps.py``;
    this pins the routes' wiring.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="A", slug="a-run-mode")
            evaluation = Evaluation(name="Eval", execution_mode="full_loop")
            s.add_all([agent, evaluation])
            await s.flush()

            def _run(mode=None):
                return EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="gpt-4.1",
                    status="completed",
                    trigger_reason="manual",
                    execution_mode=mode,
                )

            stamped, legacy = _run("llm_only"), _run()
            s.add_all([stamped, legacy])
            await s.flush()

            ran = EvaluationResult(
                run_id=legacy.id, agent_output="a", score=1.0,
                execution_details={"execution_mode": "full_loop"},
            )
            fallback = EvaluationResult(
                run_id=legacy.id, agent_output="b", score=1.0,
                execution_details={"execution_mode": "llm_only_fallback"},
            )
            s.add_all([ran, fallback])
            await s.commit()

            listed = await evaluations_api.list_runs(
                evaluation_id=str(evaluation.id), agent_id=None, model_id=None,
                limit=20, offset=0, user=admin, db=s,
            )
            modes = {r["id"]: (r["execution_mode"], r["fallback_entries"]) for r in listed}
            assert modes == {
                str(stamped.id): ("llm_only", 0),
                str(legacy.id): ("full_loop", 1),
            }

            for run, expected in (
                (stamped, {}),
                (legacy, {str(ran.id): False, str(fallback.id): True}),
            ):
                detail = await evaluations_api.get_run(run_id=str(run.id), user=admin, db=s)
                assert (detail["execution_mode"], detail["fallback_entries"]) == modes[str(run.id)]
                assert {r["id"]: r["fell_back_to_llm_only"] for r in detail["results"]} == expected
    finally:
        await cleanup()


def _evaluation_package(evaluation: dict) -> UploadFile:
    """A package holding only its evaluation: no agent, dataset or run."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr(
            "evaluation.json",
            json.dumps({"version": 1, "export_type": "evaluation_package", "evaluation": evaluation}),
        )
    return UploadFile(filename="e.zip", file=io.BytesIO(buf.getvalue()))


def test_a_mode_that_is_neither_value_is_refused_on_create_and_update():
    """An execution mode that is neither value is refused on create and update."""
    import pydantic

    for build in (
        lambda mode: CreateEvaluationRequest(name="E", execution_mode=mode),
        lambda mode: UpdateEvaluationRequest(execution_mode=mode),
    ):
        with pytest.raises(pydantic.ValidationError):
            build("full-loop")
        assert build("llm_only").execution_mode == "llm_only"
    assert CreateEvaluationRequest(name="E").execution_mode == "full_loop"
    assert UpdateEvaluationRequest().execution_mode is None


async def test_an_evaluation_created_or_imported_without_a_mode_runs_in_full_loop():
    """An evaluation created or imported without a mode runs in Full Loop; an imported mode is kept, and one that is neither value is a 422 before anything is imported."""
    from app.api.v1.evaluations import create_evaluation, import_evaluation

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await create_evaluation(
                body=CreateEvaluationRequest(name="Created"), user=admin, db=s, _agentic_gate=None
            )
            assert created["execution_mode"] == "full_loop"

            for sent, kept in ((None, "full_loop"), ("llm_only", "llm_only")):
                package = {"name": f"Imported {kept}"} | ({} if sent is None else {"execution_mode": sent})
                imported = await import_evaluation(
                    request=_asgi_request(), file=_evaluation_package(package), user=admin, db=s, _ai_gate=None
                )
                assert imported["execution_mode"] == kept

            with pytest.raises(HTTPException) as exc:
                await import_evaluation(
                    request=_asgi_request(),
                    file=_evaluation_package({"name": "Refused", "execution_mode": "full-loop"}),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            assert exc.value.status_code == 422
            assert "llm_only" in exc.value.detail and "full_loop" in exc.value.detail
            await s.commit()

            names = set((await s.execute(select(Evaluation.name))).scalars())
            assert names == {"Created", "Imported full_loop", "Imported llm_only"}
    finally:
        await cleanup()


async def test_a_copy_keeps_its_sources_mode_and_runs_an_unknown_one_as_llm_only():
    """A copy runs as its source ran: a value that is neither runs as LLM Only."""
    from app.api.v1.evaluations import copy_evaluation

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            sources = {
                mode: Evaluation(name=f"Source {mode}", user_id=admin.id, execution_mode=mode)
                for mode in ("full_loop", "llm_only", "full-loop")
            }
            s.add_all(sources.values())
            await s.commit()

            copied = {
                mode: (await copy_evaluation(evaluation_id=str(source.id), user=admin, db=s, _ai_gate=None))[
                    "execution_mode"
                ]
                for mode, source in sources.items()
            }

            assert copied == {"full_loop": "full_loop", "llm_only": "llm_only", "full-loop": "llm_only"}
    finally:
        await cleanup()


async def test_an_evaluation_holding_an_unknown_mode_exports_as_llm_only_and_imports():
    """The export writes what the evaluation runs, so its own package is never a 422."""
    from app.api.v1.evaluations import _streaming_response_to_bytes, export_evaluation, import_evaluation

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Stored before the column", user_id=admin.id, execution_mode="full-loop")
            s.add(evaluation)
            await s.commit()

            response = await export_evaluation(evaluation_id=str(evaluation.id), request=_asgi_request(), user=admin, db=s)
            package = await _streaming_response_to_bytes(response)
            exported = json.loads(zipfile.ZipFile(io.BytesIO(package)).read("evaluation.json"))
            assert exported["evaluation"]["execution_mode"] == "llm_only"

            imported = await import_evaluation(
                request=_asgi_request(),
                file=UploadFile(filename="e.zip", file=io.BytesIO(package)),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert imported["execution_mode"] == "llm_only"
    finally:
        await cleanup()


# --- the prompt guard and tool approval settings ----------------


@pytest.mark.parametrize("field", ["apply_prompt_guards", "require_tool_approval"])
async def test_a_run_setting_is_off_unless_the_author_turns_it_on(field):
    """The prompt guard and tool approval settings: create stores off
    by default or the value sent; update changes it; the response serves it."""
    from app.api.v1.evaluations import create_evaluation, update_evaluation

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            default = await create_evaluation(
                body=CreateEvaluationRequest(name="Default"), user=admin, db=s, _agentic_gate=None
            )
            on = await create_evaluation(
                body=CreateEvaluationRequest(name="On", **{field: True}),
                user=admin,
                db=s,
                _agentic_gate=None,
            )
            assert (default[field], on[field]) == (False, True)

            for sent, kept in ((True, True), (None, True), (False, False)):
                updated = await update_evaluation(
                    evaluation_id=default["id"],
                    body=UpdateEvaluationRequest(**{field: sent}),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
                assert updated[field] is kept
    finally:
        await cleanup()


@pytest.mark.parametrize("field", ["apply_prompt_guards", "require_tool_approval"])
async def test_a_run_serves_the_setting_it_recorded(field):
    """A run serves the prompt guard or tool approval setting it recorded: true,
    false, or None for a run recorded before the setting."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="A", slug=f"a-{field}")
            # The evaluation's current setting is not what a run reports.
            evaluation = Evaluation(name="Eval", execution_mode="full_loop", **{field: True})
            s.add_all([agent, evaluation])
            await s.flush()
            runs = {
                recorded: EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="gpt-4.1",
                    status="completed",
                    trigger_reason="manual",
                    **{field: recorded},
                )
                for recorded in (True, False, None)
            }
            s.add_all(runs.values())
            await s.commit()

            listed = await evaluations_api.list_runs(
                evaluation_id=str(evaluation.id), agent_id=None, model_id=None,
                limit=20, offset=0, user=admin, db=s,
            )
            served = {r["id"]: r[field] for r in listed}
            assert served == {str(run.id): recorded for recorded, run in runs.items()}
            for recorded, run in runs.items():
                detail = await evaluations_api.get_run(run_id=str(run.id), user=admin, db=s)
                assert detail[field] is recorded
    finally:
        await cleanup()


@pytest.mark.parametrize("field", ["apply_prompt_guards", "require_tool_approval"])
async def test_a_copy_keeps_its_sources_setting(field):
    """A copy keeps its source's prompt guard or tool approval setting."""
    from app.api.v1.evaluations import copy_evaluation

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            sources = {
                setting: Evaluation(name=f"Source {setting}", user_id=admin.id, **{field: setting})
                for setting in (True, False)
            }
            s.add_all(sources.values())
            await s.commit()

            for setting, source in sources.items():
                copied = await copy_evaluation(evaluation_id=str(source.id), user=admin, db=s, _ai_gate=None)
                assert copied[field] is setting
    finally:
        await cleanup()


async def test_a_package_naming_no_run_settings_imports_them_off(monkeypatch):
    """A package written before the prompt guard and tool approval
    settings imports both off on the evaluation, unknown on its runs."""
    sm, cleanup = await _make_db()
    try:
        imported = await _round_trip(sm, monkeypatch, strip_prompt_guards=True)
        async with sm() as s:
            run = await s.get(EvaluationRun, imported["ok"].run_id)
            evaluation = await s.get(Evaluation, run.evaluation_id)
        assert (evaluation.apply_prompt_guards, run.apply_prompt_guards) == (False, None)
        assert (evaluation.require_tool_approval, run.require_tool_approval) == (False, None)
    finally:
        await cleanup()


# --- the resume secret -------------------------------------------------


async def test_the_resume_secret_is_stored_encrypted_and_never_served_nor_exported():
    """The secret that signs the callbacks is a credential: encrypted at rest,
    masked in responses, kept by an update that does not send it, left out of
    the package, and an import brings none."""
    from app.api.v1.evaluations import (
        _streaming_response_to_bytes,
        create_evaluation,
        export_evaluation,
        import_evaluation,
        update_evaluation,
    )
    from app.services.evaluation.resume_secret import resume_secret

    secret = "resume-secret-5519"
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await create_evaluation(
                body=CreateEvaluationRequest(
                    name="Callbacks",
                    trigger_config={"await_callback": True, "resume_secret": secret},
                ),
                user=admin,
                db=s,
                _agentic_gate=None,
            )
            assert created["trigger_config"] == {"await_callback": True, "resume_secret": "***"}
            assert created["can_manage"] is True

            evaluation = await s.get(Evaluation, uuid.UUID(created["id"]))
            assert secret not in json.dumps(evaluation.trigger_config)
            assert resume_secret(evaluation.trigger_config) == secret

            # The config tab saves the trigger settings without the secret, or
            # sends back the mask it was served: either keeps it.
            for sent in ({"schedule": None, "on_agent_change": True}, {"resume_secret": "***"}):
                updated = await update_evaluation(
                    evaluation_id=created["id"],
                    body=UpdateEvaluationRequest(trigger_config=sent),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
                assert updated["trigger_config"]["resume_secret"] == "***"
                assert resume_secret(evaluation.trigger_config) == secret
                # And the callback mode with it: the update merges.
                assert updated["trigger_config"]["await_callback"] is True

            response = await export_evaluation(evaluation_id=created["id"], request=_asgi_request(), user=admin, db=s)
            package = await _streaming_response_to_bytes(response)
            archive = zipfile.ZipFile(io.BytesIO(package))
            assert not [name for name in archive.namelist() if secret.encode() in archive.read(name)]
            exported = json.loads(archive.read("evaluation.json"))
            assert "resume_secret" not in exported["evaluation"]["trigger_config"]

            # An older package carries the secret in clear: the
            # imported evaluation takes no callback until it is given a new one.
            imported = await import_evaluation(
                request=_asgi_request(),
                file=_evaluation_package(
                    {"name": "Imported", "trigger_config": {"await_callback": True, "resume_secret": secret}}
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            assert imported["trigger_config"] == {"await_callback": True}

            # null removes it; a value no sender can sign with, or a vault
            # reference the callback would not resolve, is a 422.
            cleared = await update_evaluation(
                evaluation_id=created["id"],
                body=UpdateEvaluationRequest(trigger_config={"resume_secret": None}),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            # Only the secret goes: the other keys are merged in, not replaced, and
            # the page's `schedule: null` removed the schedule.
            assert cleared["trigger_config"] == {"await_callback": True, "on_agent_change": True}
            for unusable in (True, "vault://kv/evaluations#resume"):
                with pytest.raises(HTTPException) as exc:
                    await update_evaluation(
                        evaluation_id=created["id"],
                        body=UpdateEvaluationRequest(trigger_config={"resume_secret": unusable}),
                        user=admin,
                        db=s,
                        _ai_gate=None,
                    )
                assert exc.value.status_code == 422
    finally:
        await cleanup()


def test_a_secret_stored_in_clear_is_neither_served_nor_exported():
    """An older row holds the secret in clear until the migration
    encrypts it: no response or package carries it meanwhile."""
    from app.services.evaluation.resume_secret import served_trigger_config, without_resume_secret

    legacy = {"await_callback": True, "resume_secret": "legacy-clear"}

    assert served_trigger_config(legacy) == {"await_callback": True}
    assert without_resume_secret(legacy) == {"await_callback": True}



# ── An import is complete and atomic ─────────────────────────────────


def _stub_agent_export(monkeypatch) -> None:
    """An agent bundle ``ImportAgentRequest`` accepts, without a real agent export."""
    import app.api.v1.agents as agents_api

    async def _stub_export_agent(*_a, **_k):
        return SimpleNamespace(
            body=json.dumps({"version": 3, "export_type": "agent", "agent": {"name": "stub"}}).encode()
        )

    monkeypatch.setattr(agents_api, "export_agent", _stub_export_agent)


async def _package_of(sm, evaluation_id, admin_id) -> bytes:
    async with sm() as s:
        response = await evaluations_api.export_evaluation(
            evaluation_id=str(evaluation_id),
            request=_asgi_request(),
            user=await s.get(User, admin_id),
            db=s,
        )
        return await evaluations_api._streaming_response_to_bytes(response)


async def _import_package(sm, admin_id, zip_bytes: bytes) -> uuid.UUID:
    async with sm() as s:
        imported = await evaluations_api.import_evaluation(
            request=_asgi_request(),
            file=UploadFile(filename="e.zip", file=io.BytesIO(zip_bytes)),
            user=await s.get(User, admin_id),
            db=s,
            _ai_gate=None,
        )
    return uuid.UUID(str(imported["id"]))


def _rewritten(zip_bytes: bytes, name: str, rewrite, *, extra: dict[str, bytes] | None = None) -> bytes:
    """*zip_bytes* with member *name* replaced by ``rewrite(its JSON)``, and *extra* added."""
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for member in src.namelist():
            data = src.read(member)
            if member == name:
                rewritten = rewrite(json.loads(data))
                data = rewritten if isinstance(rewritten, bytes) else json.dumps(rewritten).encode()
            out.writestr(member, data)
        for member, data in (extra or {}).items():
            out.writestr(member, data)
    return buf.getvalue()


def _headlines(run: EvaluationRun) -> dict:
    """A run's headlines, without the run ids its performance exclusions name: those
    are the instance's own."""
    details = dict(run.headline_details or {})
    details["performance_excluded"] = [
        {k: v for k, v in excluded.items() if k != "run_ids"}
        for excluded in details.get("performance_excluded") or []
    ]
    return {
        "score": run.score,
        "quality_score": run.quality_score,
        "performance_score": run.performance_score,
        "headline_details": details,
        "anomaly_count": run.anomaly_count,
    }


def _measured(details: dict, latency: float) -> dict:
    """*details* with a quality metric (pooled, with its counts) and a performance one."""
    return {
        **details,
        "metric_results": {
            **details["metric_results"],
            "stix.entity_prf": {"status": "scored", "value": 50.0, "detail": {"numerator": 1, "denominator": 2}},
            "performance.latency.wall_clock": {"status": "scored", "value": latency},
        },
    }


@pytest.mark.parametrize("entry_deleted", [False, True], ids=["entries-kept", "an-entry-deleted"])
async def test_an_imported_run_shows_the_headlines_of_its_source(monkeypatch, entry_deleted):
    """The package carries no headline: the import computes them from its results and
    metric rows, as a run's completion does (FR-116), with each run's scorer snapshot.
    A result whose entry was deleted still counts on the source, so it is exported and
    imported without an entry."""
    from sqlalchemy import delete

    from app.services.evaluation.metrics import log_result_metrics
    from app.services.evaluation_engine import _compute_run_headlines

    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        async with sm() as s:
            judged = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.evaluation_id == evaluation_id))
            ).scalar_one()
            judged.metric_suite_version = "13.0.0"
            # Judge off: its results store 0.0, and its judge headline is N/A.
            judge_off = EvaluationRun(
                evaluation_id=evaluation_id,
                agent_id=judged.agent_id,
                model_id="judge-off",
                status="completed",
                trigger_reason="manual",
                total_entries=3,
                scorer_selection=[],
            )
            s.add(judge_off)
            await s.flush()
            for result in (
                await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == judged.id))
            ).scalars().all():
                s.add(
                    EvaluationResult(
                        run_id=judge_off.id,
                        entry_id=result.entry_id,
                        agent_output=result.agent_output,
                        score=0.0,
                        sub_scores={},
                        execution_details=_measured(result.execution_details, 4.0),
                    )
                )
                result.execution_details = _measured(result.execution_details, 2.0)
            await s.flush()
            for result in (
                await s.execute(
                    select(EvaluationResult).join(EvaluationRun).where(EvaluationRun.evaluation_id == evaluation_id)
                )
            ).scalars().all():
                await log_result_metrics(
                    s,
                    result_id=result.id,
                    run_id=result.run_id,
                    execution_details=result.execution_details,
                    is_reused=result.is_reused,
                )
            if entry_deleted:
                # The judged run's only valid result: without it the judge headline is N/A.
                valid = (
                    await s.execute(
                        select(EvaluationResult).where(
                            EvaluationResult.run_id == judged.id, EvaluationResult.agent_output == "ok"
                        )
                    )
                ).scalar_one()
                await s.execute(delete(DatasetEntry).where(DatasetEntry.id == valid.entry_id))
            await s.commit()
            await _compute_run_headlines(s, evaluation_id)
            source = {
                run.model_id: _headlines(run)
                for run in (
                    await s.execute(select(EvaluationRun).where(EvaluationRun.evaluation_id == evaluation_id))
                ).scalars()
            }

        imported_id = await _import_package(sm, admin_id, await _package_of(sm, evaluation_id, admin_id))

        async with sm() as s:
            imported_runs = (
                await s.execute(select(EvaluationRun).where(EvaluationRun.evaluation_id == imported_id))
            ).scalars().all()
            without_entry = (
                await s.execute(
                    select(EvaluationResult.agent_output)
                    .join(EvaluationRun)
                    .where(EvaluationRun.evaluation_id == imported_id, EvaluationResult.entry_id.is_(None))
                )
            ).scalars().all()
        assert {run.model_id: _headlines(run) for run in imported_runs} == source
        assert sorted(without_entry) == (["ok", "ok"] if entry_deleted else [])
        assert source["m"]["score"] is not None
        assert all(run["quality_score"] is not None for run in source.values())
        assert source["m"]["performance_score"] > source["judge-off"]["performance_score"]
        assert source["judge-off"]["score"] is None
        assert {run.model_id: run.scorer_selection for run in imported_runs} == {"m": None, "judge-off": []}
        assert {run.model_id: run.metric_suite_version for run in imported_runs} == {"m": "13.0.0", "judge-off": None}
    finally:
        await cleanup()


async def test_a_package_named_like_several_evaluations_imports_under_a_new_name(monkeypatch):
    """The name lookup was a 500 once two evaluations shared the package's name."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        name = json.loads(zipfile.ZipFile(io.BytesIO(package)).read("evaluation.json"))["evaluation"]["name"]
        async with sm() as s:
            s.add(Evaluation(name=name.upper()))
            await s.commit()

        imported_id = await _import_package(sm, admin_id, package)

        async with sm() as s:
            imported = await s.get(Evaluation, imported_id)
        assert imported.name.startswith(f"{name}-") and len(imported.name) == len(name) + 9
    finally:
        await cleanup()


_IMPORTED = (Agent, Dataset, DatasetEntry, Evaluation, EvaluationRun, EvaluationResult, EvaluationMetric)


async def _row_counts(sm, models=_IMPORTED) -> dict[str, int]:
    from sqlalchemy import func

    async with sm() as s:
        return {
            model.__tablename__: (await s.execute(select(func.count()).select_from(model))).scalar_one()
            for model in models
        }


def _with_a_dataset_file(package: bytes) -> bytes:
    """The package with a file on its dataset's first entry: importing it stores an object."""
    import hashlib

    def _add_file(manifest: dict) -> bytes:
        manifest["files"] = [
            {
                "id": "f0",
                "filename": "a.txt",
                "content_type": "text/plain",
                "path": "files/f0_a.txt",
                "sha256": hashlib.sha256(b"hi").hexdigest(),
            }
        ]
        manifest["entries"][0]["files"] = ["f0"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            out.writestr("manifest.json", json.dumps(manifest))
            out.writestr("files/f0_a.txt", b"hi")
        return buf.getvalue()

    dataset_zip = zipfile.ZipFile(io.BytesIO(package)).read("dependencies/datasets/dataset-0.zip")
    rebuilt = _add_file(json.loads(zipfile.ZipFile(io.BytesIO(dataset_zip)).read("manifest.json")))
    return _replace_member(package, "dependencies/datasets/dataset-0.zip", rebuilt)


def _replace_member(zip_bytes: bytes, name: str, data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for member in src.namelist():
            out.writestr(member, data if member == name else src.read(member))
    return buf.getvalue()


def _a_second_dataset_refused(package: bytes) -> bytes:
    """A second embedded dataset its import refuses: the import fails after the agent,
    the first dataset and its file are written."""

    def _add_dependency(definition: dict) -> dict:
        definition["dependencies"]["datasets"].append(
            {"index": 1, "position": 1, "path": "dependencies/datasets/dataset-1.zip"}
        )
        return definition

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        out.writestr("manifest.json", json.dumps({"format": "not-a-dataset"}))
    return _rewritten(
        package,
        "evaluation.json",
        _add_dependency,
        extra={"dependencies/datasets/dataset-1.zip": buf.getvalue()},
    )


@pytest.mark.parametrize("failure", ["a-later-dataset", "an-agent", "the-headlines", "the-commit"])
async def test_a_package_failing_halfway_leaves_nothing_behind(monkeypatch, failure):
    """One transaction for the whole import, its agents and datasets included, and the
    files its datasets stored deleted: a retry does not find half a package."""
    from app.models.file_attachment import FileAttachment
    from app.services import evaluation_engine
    from app.services import file_storage as file_storage_module

    stored: list[str] = []
    deleted: list[str] = []

    async def _upload(**_kwargs):
        stored.append(f"dataset/import/{uuid.uuid4().hex}")
        return stored[-1]

    async def _delete(object_name):
        deleted.append(object_name)

    monkeypatch.setattr(file_storage_module.file_storage, "upload", _upload)
    monkeypatch.setattr(file_storage_module.file_storage, "delete", _delete)
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db_full()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = _with_a_dataset_file(await _package_of(sm, evaluation_id, admin_id))
        if failure == "a-later-dataset":
            package = _a_second_dataset_refused(package)
        elif failure == "an-agent":
            # Accepted by ``ImportAgentRequest``, refused by the agent import, which
            # runs after the datasets and their files.
            package = _rewritten(
                package, "dependencies/agents/agent-0.json", lambda _: {"version": 3, "agent": {"name": ""}}
            )
        elif failure == "the-headlines":

            async def _fail(*_a, **_k):
                raise RuntimeError("headlines failed")

            monkeypatch.setattr(evaluation_engine, "_compute_run_headlines", _fail)
        else:
            from sqlalchemy.ext.asyncio import AsyncSession

            commit = AsyncSession.commit

            async def _fail_once(_session):
                monkeypatch.setattr(AsyncSession, "commit", commit)
                raise RuntimeError("commit failed")

            monkeypatch.setattr(AsyncSession, "commit", _fail_once)
        tables = (*_IMPORTED, FileAttachment)
        before = await _row_counts(sm, tables)

        with pytest.raises((HTTPException, RuntimeError)):
            await _import_package(sm, admin_id, package)

        assert await _row_counts(sm, tables) == before
        assert stored and deleted == stored
    finally:
        await cleanup()


async def test_a_failed_deletion_neither_stops_the_others_nor_hides_the_error(monkeypatch):
    """The cleanup is best-effort per object: a transport error deleting one
    file leaves the next one deleted and the block's own error raised."""
    import asyncio

    from app.services import file_storage as file_storage_module

    deleted: list[str] = []

    async def _delete(object_name):
        if object_name == "a":
            raise ConnectionError("storage unreachable")
        deleted.append(object_name)

    monkeypatch.setattr(file_storage_module.file_storage, "delete", _delete)

    class _Session:
        rolled_back = 0

        async def rollback(self):
            self.rolled_back += 1

    for error in (ValueError("the import failed"), asyncio.CancelledError()):
        session, deleted[:] = _Session(), []
        with pytest.raises(type(error)):
            async with file_storage_module.objects_deleted_on_failure(session) as stored:
                stored += ["a", "b"]
                raise error
        assert session.rolled_back == 1
        assert deleted == ["b"]


async def test_an_embedded_dataset_with_tied_positions_is_refused(monkeypatch):
    """The evaluation import finds a result's entry by its position: a tie
    loses which entry a result answered, so the package is refused, not guessed."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        dataset_zip = zipfile.ZipFile(io.BytesIO(package)).read("dependencies/datasets/dataset-0.zip")
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(dataset_zip)).read("manifest.json"))
        manifest["entries"].append({**manifest["entries"][0], "input_prompt": "tied"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            out.writestr("manifest.json", json.dumps(manifest))
        package = _replace_member(package, "dependencies/datasets/dataset-0.zip", buf.getvalue())
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, package)

        assert refused.value.status_code == 422
        assert refused.value.detail.startswith("dependencies/datasets/dataset-0.zip: (entry 3) position 0")
        assert "is also entry 0's, and a result names it" in refused.value.detail
        assert await _row_counts(sm) == before
    finally:
        await cleanup()


async def test_a_tie_no_result_names_is_renumbered_and_each_result_keeps_its_entry(monkeypatch):
    """A tie loses nothing when no result names its position: the entries are
    renumbered, and each result still lands on the entry it answered, found by its
    position in the package, not the renumbered one."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        dataset_zip = zipfile.ZipFile(io.BytesIO(package)).read("dependencies/datasets/dataset-0.zip")
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(dataset_zip)).read("manifest.json"))
        # The scored entries move to 3, 4, 5, behind two unscored entries tied at 0:
        # renumbered, they land at 2, 3, 4.
        for entry in manifest["entries"]:
            entry["input_prompt"] = f"scored at {entry['position']}"
            entry["position"] += 3
        manifest["entries"] += [
            {**manifest["entries"][0], "position": 0, "input_prompt": f"unscored {i}"} for i in range(2)
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            out.writestr("manifest.json", json.dumps(manifest))
        package = _replace_member(package, "dependencies/datasets/dataset-0.zip", buf.getvalue())

        def _shift(payload: dict) -> dict:
            for result in payload["results"]:
                result["entry_ref"]["entry_position"] += 3
            return payload

        package = _rewritten(package, "evaluation_runs/run-0.json", _shift)
        imported_id = await _import_package(sm, admin_id, package)

        async with sm() as s:
            entries = (
                await s.execute(
                    select(DatasetEntry.input_prompt, DatasetEntry.position)
                    .join(EvaluationDataset, EvaluationDataset.dataset_id == DatasetEntry.dataset_id)
                    .where(EvaluationDataset.evaluation_id == imported_id)
                    .order_by(DatasetEntry.position)
                )
            ).all()
            answered = dict(
                (
                    await s.execute(
                        select(EvaluationResult.agent_output, DatasetEntry.input_prompt)
                        .join(DatasetEntry, DatasetEntry.id == EvaluationResult.entry_id)
                        .join(EvaluationRun)
                        .where(EvaluationRun.evaluation_id == imported_id)
                    )
                ).all()
            )
        assert [tuple(row) for row in entries] == [
            ("unscored 0", 0),
            ("unscored 1", 1),
            ("scored at 0", 2),
            ("scored at 1", 3),
            ("scored at 2", 4),
        ]
        assert answered == {"ok": "scored at 0", "junk": "scored at 1", "Error: 503": "scored at 2"}
    finally:
        await cleanup()


async def test_the_cleanup_finishes_when_the_task_is_cancelled_again(monkeypatch):
    """A real cancellation: the task can be cancelled again while it cleans up, and its
    next ``await`` would then raise. The rollback and every deletion still finish,
    and the task ends cancelled."""
    from app.services import file_storage as file_storage_module

    deleted: list[str] = []

    async def _delete(object_name):
        await asyncio.sleep(0)
        deleted.append(object_name)

    monkeypatch.setattr(file_storage_module.file_storage, "delete", _delete)
    importing, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class _Session:
        rolled_back = False

        async def rollback(self):
            rolling_back.set()
            await release.wait()
            self.rolled_back = True

    session = _Session()

    async def _import():
        async with file_storage_module.objects_deleted_on_failure(session) as stored:
            stored += ["a", "b"]
            importing.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_import())
    await importing.wait()
    task.cancel()
    await rolling_back.wait()
    task.cancel()  # a second cancellation, while the rollback runs
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.rolled_back
    assert deleted == ["a", "b"]


async def test_a_failed_rollback_neither_keeps_the_files_nor_hides_the_error(monkeypatch):
    """A rollback that fails (a dead connection) still leaves the files deleted and the
    block's own error raised."""
    from app.services import file_storage as file_storage_module

    deleted: list[str] = []

    async def _delete(object_name):
        deleted.append(object_name)

    monkeypatch.setattr(file_storage_module.file_storage, "delete", _delete)

    class _DeadSession:
        async def rollback(self):
            raise ConnectionError("connection lost")

    with pytest.raises(ValueError, match="the import failed"):
        async with file_storage_module.objects_deleted_on_failure(_DeadSession()) as stored:
            stored += ["a", "b"]
            raise ValueError("the import failed")
    assert deleted == ["a", "b"]


async def test_a_nested_dataset_package_too_large_uncompressed_is_refused(monkeypatch):
    """The outer package's cap counts a nested dataset package as one compressed member:
    each one is held to the cap on what its own members declare."""
    from app.api.v1 import datasets as datasets_api

    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        nested = zipfile.ZipFile(
            io.BytesIO(zipfile.ZipFile(io.BytesIO(package)).read("dependencies/datasets/dataset-0.zip"))
        )
        declared = sum(member.file_size for member in nested.infolist())
        monkeypatch.setattr(datasets_api, "MAX_UNCOMPRESSED_SIZE", declared - 1)
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, package)

        assert refused.value.status_code == 413
        assert "uncompressed" in refused.value.detail
        assert await _row_counts(sm) == before
    finally:
        await cleanup()


def _evaluation_json(change):
    """A package change: *change* applied to ``evaluation.json``."""

    def _apply(package: bytes) -> bytes:
        def _rewrite(definition: dict):
            return change(definition) or definition

        return _rewritten(package, "evaluation.json", _rewrite)

    return _apply


def _run_results(change):
    """A package change: *change* applied to the first run's results file."""

    def _apply(package: bytes) -> bytes:
        def _rewrite(payload: dict):
            return change(payload) or payload

        return _rewritten(package, "evaluation_runs/run-0.json", _rewrite)

    return _apply


def _set(path: str, value):
    """Set the value at a dotted *path* (list indexes as numbers)."""

    def _change(document: dict) -> None:
        *parents, last = path.split(".")
        node = document
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]
        node[int(last) if isinstance(node, list) else last] = value

    return _change


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (_evaluation_json(lambda _: ["not", "an", "object"]), "evaluation.json: not a JSON object"),
        (_evaluation_json(_set("version", "one")), "version"),
        (_evaluation_json(_set("evaluation", "not an object")), "evaluation: Input should be an object"),
        (_evaluation_json(_set("evaluation.name", 42)), "evaluation.name"),
        (_evaluation_json(_set("evaluation.last_triggered_at", 1700000000)), "evaluation.last_triggered_at"),
        (_evaluation_json(_set("dependencies.datasets.0.index", "first")), "dependencies.datasets.0.index"),
        (_evaluation_json(_set("dependencies.datasets.0.position", 1.5)), "dependencies.datasets.0.position"),
        (_evaluation_json(_set("dependencies.agents.0.index", -1)), "dependencies.agents.0.index"),
        (
            _evaluation_json(
                lambda d: d["dependencies"]["datasets"].append(dict(d["dependencies"]["datasets"][0]))
            ),
            "datasets: index 0 is used twice",
        ),
        (
            _evaluation_json(
                lambda d: d["dependencies"]["datasets"].append({**d["dependencies"]["datasets"][0], "index": 1})
            ),
            "datasets: position 0 is used twice",
        ),
        (
            _evaluation_json(lambda d: d["dependencies"]["agents"].append(dict(d["dependencies"]["agents"][0]))),
            "agents: index 0 is used twice",
        ),
        (_evaluation_json(_set("evaluation_runs.0", "not a run")), "evaluation_runs.0: Input should be an object"),
        (_evaluation_json(_set("evaluation_runs.0.agent_index", 1.5)), "evaluation_runs.0.agent_index"),
        (_evaluation_json(_set("evaluation_runs.0.completed_entries", "many")), "evaluation_runs.0.completed_entries"),
        (_evaluation_json(_set("evaluation_runs.0.total_entries", 2**31)), "evaluation_runs.0.total_entries"),
        (_evaluation_json(_set("evaluation_runs.0.duration_seconds", "slow")), "evaluation_runs.0.duration_seconds"),
        (_evaluation_json(_set("evaluation_runs.0.model_id", "m" * 101)), "evaluation_runs.0.model_id"),
        (_evaluation_json(_set("evaluation_runs.0.trigger_reason", ["manual"])), "evaluation_runs.0.trigger_reason"),
        (_evaluation_json(_set("evaluation_runs.0.persona_version_number", "v2")), "evaluation_runs.0.persona_version_number"),
        (_evaluation_json(_set("evaluation_runs.0.prompt_versions", ["p"])), "evaluation_runs.0.prompt_versions"),
        (_evaluation_json(_set("evaluation_runs.0.scorer_selection", "judge")), "evaluation_runs.0.scorer_selection"),
        (_evaluation_json(_set("evaluation_runs.0.error", "\ud800")), "evaluation_runs.0.error"),
        (_run_results(_set("results.0.score", "high")), "results.0.score"),
        (_run_results(_set("results.0.entry_ref", "entry-0")), "results.0.entry_ref"),
        (_run_results(_set("results.0.entry_ref.entry_position", None)), "results.0.entry_ref.entry_position"),
        # A ref the export never writes: it names no entry of the package.
        (
            _run_results(_set("results.0.entry_ref.entry_position", 99)),
            "Invalid evaluation_runs/run-0.json: results.0.entry_ref: names no entry of the package",
        ),
        (_run_results(_set("results.0.entry_ref.dataset_index", 7)), "results.0.entry_ref: names no entry"),
        (_run_results(_set("results.0.agent_output", {"text": "ok"})), "results.0.agent_output"),
        (_run_results(_set("results.0.sub_scores", [1])), "results.0.sub_scores"),
        (_run_results(_set("results.0.execution_anomaly_kind", "k" * 100)), "results.0.execution_anomaly_kind"),
        (_run_results(lambda _: b"not json"), "evaluation_runs/run-0.json"),
        (_run_results(lambda _: ["results"]), "evaluation_runs/run-0.json: not a JSON object"),
    ],
)
async def test_a_malformed_package_is_a_400_naming_the_field(monkeypatch, change, field):
    """A hand-edited or third-party package is refused with the field at fault, as a bad
    ZIP or bad JSON is, never a 500, and nothing is written."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = change(await _package_of(sm, evaluation_id, admin_id))
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, package)

        assert refused.value.status_code == 400
        assert field in refused.value.detail
        assert await _row_counts(sm) == before
    finally:
        await cleanup()


async def test_a_run_names_its_agent_by_the_agents_index(monkeypatch):
    """``agent_index`` is the ``index`` of an embedded agent, not a place in the list:
    a run naming no embedded agent is skipped."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)

        def _renumber(definition: dict) -> None:
            definition["dependencies"]["agents"][0]["index"] = 5
            run = definition["evaluation_runs"][0]
            definition["evaluation_runs"] = [
                {**run, "agent_index": 0, "model_id": "names-no-agent"},
                {**run, "agent_index": 5, "model_id": "names-the-agent"},
            ]

        imported_id = await _import_package(
            sm, admin_id, _evaluation_json(_renumber)(await _package_of(sm, evaluation_id, admin_id))
        )

        async with sm() as s:
            (agent_id,) = (
                await s.execute(select(EvaluationAgent.agent_id).where(EvaluationAgent.evaluation_id == imported_id))
            ).scalars().all()
            runs = (
                await s.execute(
                    select(EvaluationRun.model_id, EvaluationRun.agent_id).where(
                        EvaluationRun.evaluation_id == imported_id
                    )
                )
            ).all()
        assert [tuple(run) for run in runs] == [("names-the-agent", agent_id)]
    finally:
        await cleanup()


async def test_a_member_declaring_less_than_it_holds_is_refused_without_being_held():
    """A member's declared size is set by whoever built the package: an
    ``evaluation.json`` declaring 10 bytes and holding 64 MiB is refused as corrupt,
    having decompressed what it declared, where reading it whole held it twice over."""
    from tests.archive_helpers import declare_size, peak_mib_of

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("evaluation.json", b"\0" * (64 * 2**20))
    package = declare_size(buf.getvalue(), "evaluation.json", 10)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            peak, refused = await peak_mib_of(
                evaluations_api.import_evaluation(
                    request=_asgi_request(),
                    file=UploadFile(filename="e.zip", file=io.BytesIO(package)),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            )
        assert isinstance(refused, HTTPException)
        assert refused.status_code == 400
        assert refused.detail == "Invalid ZIP file"
        assert peak <= 32
    finally:
        await cleanup()


async def test_an_evaluation_json_past_the_json_member_cap_is_a_413(monkeypatch):
    """A JSON member is parsed whole, so it has a cap of its own within the budget."""
    monkeypatch.setattr(evaluations_api, "_MAX_PACKAGE_JSON_MEMBER_BYTES", 10)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as refused:
                await evaluations_api.import_evaluation(
                    request=_asgi_request(),
                    file=_evaluation_package({"name": "Capped"}),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
        assert refused.value.status_code == 413
        assert "evaluation.json" in refused.value.detail
    finally:
        await cleanup()


async def test_an_agent_bundle_past_the_json_member_cap_is_a_413(monkeypatch):
    """An agent bundle is held to the cap ``evaluation.json`` is, not to the results' room."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        cap = zipfile.ZipFile(io.BytesIO(package)).getinfo("evaluation.json").file_size
        monkeypatch.setattr(evaluations_api, "_MAX_PACKAGE_JSON_MEMBER_BYTES", cap)
        agent = "dependencies/agents/agent-0.json"
        # Whitespace after the document: still the same JSON, now past the cap.
        padded = _rewritten(package, agent, lambda bundle: json.dumps(bundle).encode() + b" " * (cap + 1))
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, padded)

        assert refused.value.status_code == 413
        assert agent in refused.value.detail
        assert await _row_counts(sm) == before
    finally:
        await cleanup()


async def test_a_runs_results_are_not_held_to_the_json_member_cap(monkeypatch):
    """The results carry the traces, so they keep the room the other members are denied."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        cap = max(
            m.file_size
            for m in zipfile.ZipFile(io.BytesIO(package)).infolist()
            if m.filename != "evaluation_runs/run-0.json"
        )
        monkeypatch.setattr(evaluations_api, "_MAX_PACKAGE_JSON_MEMBER_BYTES", cap)
        # Whitespace after the document: still the same JSON, now past the cap.
        padded = _rewritten(
            package, "evaluation_runs/run-0.json", lambda payload: json.dumps(payload).encode() + b" " * (cap + 1)
        )

        imported_id = await _import_package(sm, admin_id, padded)

        async with sm() as s:
            runs = (await s.scalars(select(EvaluationRun).where(EvaluationRun.evaluation_id == imported_id))).all()
        assert len(runs) == 1
    finally:
        await cleanup()


async def test_a_nested_dataset_package_spends_the_evaluations_budget(monkeypatch):
    """One budget bounds the whole import: with room for the evaluation's own
    members and one byte more, the nested package's manifest is past it."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        declared = sum(member.file_size for member in zipfile.ZipFile(io.BytesIO(package)).infolist())
        monkeypatch.setattr(evaluations_api, "_MAX_PACKAGE_UNCOMPRESSED_BYTES", declared + 1)
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, package)

        assert refused.value.status_code == 413
        assert refused.value.detail.startswith("dependencies/datasets/dataset-0.zip: ")
        assert "manifest.json" in refused.value.detail
        assert await _row_counts(sm) == before
    finally:
        await cleanup()


@pytest.mark.parametrize("limit", ["_MAX_PACKAGE_BYTES", "_MAX_PACKAGE_UNCOMPRESSED_BYTES"])
async def test_a_package_past_its_limits_is_a_413(monkeypatch, limit):
    """The import holds the package whole while it checks it, so its size is bounded,
    uploaded and as its members declare it uncompressed."""
    _stub_agent_export(monkeypatch)
    sm, cleanup = await _make_db()
    try:
        evaluation_id, admin_id = await _export_fixture(sm)
        package = await _package_of(sm, evaluation_id, admin_id)
        uncompressed = sum(member.file_size for member in zipfile.ZipFile(io.BytesIO(package)).infolist())
        monkeypatch.setattr(
            evaluations_api, limit, len(package) - 1 if limit == "_MAX_PACKAGE_BYTES" else uncompressed - 1
        )
        before = await _row_counts(sm)

        with pytest.raises(HTTPException) as refused:
            await _import_package(sm, admin_id, package)

        assert refused.value.status_code == 413
        assert await _row_counts(sm) == before
    finally:
        await cleanup()

