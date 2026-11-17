"""Evaluation API request and response shapes: the metrics-aggregate schema, the bulk
``run_ids`` cap, the headline exclusion vocabulary shared with the frontend, chart
inclusion of model-independent runs, read-time effective tags and headline provenance
on runs, the overall metric selections on evaluations, and whether the reader may
manage an evaluation.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.evaluations import _build_run_response, get_chart_data, get_result_metrics
from app.api.v1.schemas.evaluation import (
    MAX_BULK_RUN_IDS,
    MetricResponse,
    RescoreFromRunsRequest,
    RunMetricsAggregateResponse,
    TriggerEvaluationFromRunsRequest,
)
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.flow import Flow
from app.models.user import User
from app.services.evaluation.constants import MODEL_INDEPENDENT
from tests.scratch_database import ALL_TABLES, scratch_database

# ── Metrics-aggregate response model ──
# `GET /evaluations/runs/{id}/metrics/aggregate` has a `response_model`, so Pydantic
# silently discards any key the schema does not declare. The run page groups its Metrics
# panel by `half`, which the client cannot infer from the key; without it the panel
# renders empty on a 200. The same holds for `pooled` and `unit`, and for the per-result
# metrics response.


def _aggregate_row(**fields) -> dict:
    """One row of `GET /evaluations/runs/{id}/metrics/aggregate`, through its response model."""
    return RunMetricsAggregateResponse.model_validate(
        {"run_id": "r1", "aggregates": [{"key": "stix.entity_prf", "count": 2, **fields}]}
    ).model_dump()["aggregates"][0]


def _result_row(**fields) -> dict:
    """One row of `GET .../results/{id}/metrics`, through its response model."""
    return MetricResponse.model_validate({"key": "stix.entity_prf", "value": 31.43, **fields}).model_dump()


@pytest.mark.parametrize(
    ("serialize", "field", "value", "default"),
    [
        # The Metrics panels group by `half`; a row without it composes neither headline.
        (_aggregate_row, "half", "quality", None),
        (_aggregate_row, "half", "performance", None),
        (_aggregate_row, "half", None, None),
        (_result_row, "half", "quality", None),
        (_result_row, "half", "performance", None),
        (_result_row, "half", None, None),
        # The panel notes a pooled metric's run value; a row without it is averaged.
        (_aggregate_row, "pooled", True, False),
        (_aggregate_row, "pooled", False, False),
        # The client formats a value by its unit; a row without it has none declared.
        (_aggregate_row, "unit", "milliseconds", None),
        (_result_row, "unit", "milliseconds", None),
        # An entry's Metrics strip follows how a metric reads on one entry; a row
        # without it is shown under its own name.
        (_result_row, "shown_on_entry", False, True),
        (_result_row, "entry_label", "Label F1", None),
    ],
    ids=[
        "aggregate-half-quality",
        "aggregate-half-performance",
        "aggregate-half-none",
        "result-half-quality",
        "result-half-performance",
        "result-half-none",
        "aggregate-pooled",
        "aggregate-not-pooled",
        "aggregate-unit",
        "result-unit",
        "result-not-shown-on-entry",
        "result-entry-label",
    ],
)
def test_a_metric_row_field_survives_the_response_model(serialize, field, value, default):
    """Each field reaches the wire intact, and a historical row without it reads as its default."""
    row = serialize(**{field: value})
    assert field in row, f"the response model dropped `{field}`"
    assert row[field] == value
    assert type(row[field]) is type(value)

    historical = serialize()
    assert historical[field] == default
    assert type(historical[field]) is type(default)


def test_no_run_payload_key_is_silently_dropped_by_the_response_model():
    """Every key `_build_run_response` builds is declared on the schema.

    Pydantic's default `extra="ignore"` drops an undeclared key silently, and a
    hand-written field list only catches the drops someone remembered to list
    (`metric_suite_version`, `scorer_selection`). The payload's keys
    are read from the source so a new field cannot be missed.
    """
    import ast

    from app.api.v1.schemas.evaluation import EvaluationRunResponse
    from tests.evaluation_helpers import BACKEND_DIR

    source = (BACKEND_DIR / "app" / "api" / "v1" / "evaluations.py").read_text(
        encoding="utf-8"
    )
    builder = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_run_response"
    )
    returns = [node for node in ast.walk(builder) if isinstance(node, ast.Return)]
    payload = returns[-1].value
    assert isinstance(payload, ast.Dict), (
        "`_build_run_response` no longer returns a dict literal; this guard "
        "reads the payload's keys statically and must be updated with it"
    )

    keys = [k.value for k in payload.keys if isinstance(k, ast.Constant)]
    assert len(keys) == len(payload.keys), (
        "the payload has a non-literal key, which this guard cannot see"
    )

    dropped = [k for k in keys if k not in EvaluationRunResponse.model_fields]
    assert not dropped, (
        f"`_build_run_response` builds {dropped} but `EvaluationRunResponse` "
        "does not declare them, so Pydantic discards them before the client "
        "sees them. Declare them on the schema."
    )


# ── Bulk run_ids cap ──
# ``run_ids`` is bounded on both bulk endpoints by one shared ``max_length`` constant,
# so an oversized batch is rejected before any row lock is taken or any query issued.


@pytest.mark.parametrize(
    "schema_cls", [TriggerEvaluationFromRunsRequest, RescoreFromRunsRequest]
)
def test_run_ids_at_cap_is_accepted(schema_cls):
    run_ids = [str(uuid.uuid4()) for _ in range(MAX_BULK_RUN_IDS)]

    instance = schema_cls(run_ids=run_ids)

    assert len(instance.run_ids) == MAX_BULK_RUN_IDS


@pytest.mark.parametrize(
    "schema_cls", [TriggerEvaluationFromRunsRequest, RescoreFromRunsRequest]
)
def test_run_ids_over_cap_is_rejected(schema_cls):
    run_ids = [str(uuid.uuid4()) for _ in range(MAX_BULK_RUN_IDS + 1)]

    with pytest.raises(ValidationError):
        schema_cls(run_ids=run_ids)


# ── Headline exclusion vocabulary shared with the frontend ──
# The backend's ``REASON_*`` constants and the frontend's ``KnownExclusionReason`` type
# live in two languages, so only a test that reads both can hold them together. The check
# is literal presence in ``frontend/src/types/index.ts``, not TypeScript control flow.
# Rendering is tested by ``frontend/src/pages/EvaluationRunDetail.metric-rows.test.tsx``.


_TYPES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "index.ts"


def test_every_reason_constant_is_declared_on_both_sides():
    """A reason the backend can emit must be declared to the browser.

    The sentinel is included because ``buildMetricRows`` filters on it: without this pin
    the two spellings can drift and a run-level entry renders as a metric row.
    """
    from app.services.evaluation import aggregation

    assert _TYPES.exists(), f"frontend source not found: {_TYPES}"
    source = _TYPES.read_text(encoding="utf-8")
    reasons = {
        name: getattr(aggregation, name)
        for name in dir(aggregation)
        if name.startswith("REASON_")
    }
    assert reasons, "no REASON_* constants found -- the pin would be vacuous"

    missing = [
        f"{name}={value!r}" for name, value in reasons.items() if f'"{value}"' not in source
    ]
    assert not missing, f"not declared in types/index.ts: {missing}"

    sentinel = aggregation.RUN_LEVEL_EXCLUSION_KEY
    assert f'"{sentinel}"' in source, "RUN_LEVEL_EXCLUSION_KEY is not declared in TypeScript"


# ── Charts include model-independent runs ──
# A ``skip_llm`` agent's run carries the ``MODEL_INDEPENDENT`` sentinel in ``model_id``
# and is measured like any other run; excluding it from ``get_chart_data`` would let the
# overview name a lower-scoring run as the leader.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_model_independent_run_is_charted_like_any_other():
    async with contextlib.AsyncExitStack() as stack:
        db = await stack.enter_async_context(
            scratch_database("chartskip", tables=ALL_TABLES, extensions=("vector",))
        )
        async with db.sessions() as session:
            skip_llm_agent = Agent(name="Rules")
            llm_agent = Agent(name="Chat")
            evaluation = Evaluation(name="Eval")
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name="Admin",
                role="admin",
            )
            session.add_all([skip_llm_agent, llm_agent, evaluation, admin])
            await session.flush()
            for agent, model_id, quality, performance in (
                (skip_llm_agent, MODEL_INDEPENDENT, 90.0, 80.0),
                (llm_agent, "gpt-4o", 50.0, 40.0),
            ):
                session.add(
                    EvaluationRun(
                        evaluation_id=evaluation.id,
                        agent_id=agent.id,
                        model_id=model_id,
                        trigger_reason="manual",
                        status="completed",
                        quality_score=quality,
                        performance_score=performance,
                    )
                )
            await session.commit()

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

    skip_series = f"Rules/{MODEL_INDEPENDENT}"
    quality = {k: v for p in chart["trends"]["quality"] for k, v in p["scores"].items()}
    performance = {
        k: v for p in chart["trends"]["performance"] for k, v in p["scores"].items()
    }
    assert quality == {skip_series: 90.0, "Chat/gpt-4o": 50.0}
    assert performance == {skip_series: 80.0, "Chat/gpt-4o": 40.0}
    assert chart["total_runs"] == 2
    point = next(
        p
        for p in chart["correlations"]["quality_performance"]
        if p["model_id"] == MODEL_INDEPENDENT
    )
    assert (point["x"], point["y"]) == (90.0, 80.0)


# ── Read-time effective tags and baseline on runs ──
# An evaluation-level tag appears in an existing run's ``effective_tags`` and follows
# later changes without mutating the run's persisted ``tags`` (a run tag wins on the same
# key); ``is_baseline`` is derived from ``EvaluationAgent.is_baseline`` at read time.


_TABLES = [
    User.__table__,
    Agent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    EvaluationRun.__table__,
    EvaluationResult.__table__,
]


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("efftags", tables=_TABLES))
    return db.sessions, stack.aclose


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evaluation_tag_appears_in_run_effective_tags_retroactively():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ev = Evaluation(name="Eval")
            agent = Agent(name="A")
            s.add_all([ev, agent])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=ev.id, agent_id=agent.id))
            run = EvaluationRun(
                evaluation_id=ev.id,
                agent_id=agent.id,
                model_id="claude-sonnet-4",
                trigger_reason="manual",
                tags={"note": "x"},
            )
            s.add(run)
            await s.commit()

            # No evaluation tag yet, agent flag unset.
            resp = await _build_run_response(s, run, missing_snapshots=[])
            assert resp["effective_tags"] == {"note": "x"}
            assert resp["is_baseline"] is False

            # ``note`` collides with the run's own tag: the run's value wins.
            ev.tags = {"campaign": "spring", "note": "eval"}
            ea = (
                await s.execute(
                    select(EvaluationAgent).where(
                        EvaluationAgent.evaluation_id == ev.id,
                        EvaluationAgent.agent_id == agent.id,
                    )
                )
            ).scalar_one()
            ea.is_baseline = True
            await s.commit()

            # The pre-existing run reflects both changes at read time, its own tag
            # taking precedence over the evaluation's...
            resp2 = await _build_run_response(s, run, missing_snapshots=[])
            assert resp2["effective_tags"] == {"campaign": "spring", "note": "x"}
            assert resp2["is_baseline"] is True

            # ...but its persisted tags were never mutated.
            persisted = (
                await s.execute(
                    select(EvaluationRun.tags).where(EvaluationRun.id == run.id)
                )
            ).scalar_one()
            assert persisted == {"note": "x"}
    finally:
        await cleanup()


# ── Headline provenance on run responses ──
# `_build_run_response` is the only place a run's stored headline provenance becomes wire
# data. Both halves of the exclusion record must survive: the two headlines are composed
# by different code at different times, and a metric missing from one says nothing about
# the other.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_both_exclusion_halves_reach_the_wire():
    """Quality AND performance exclusions are both serialised."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ev = Evaluation(name="Eval")
            agent = Agent(name="A")
            s.add_all([ev, agent])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=ev.id, agent_id=agent.id))
            run = EvaluationRun(
                evaluation_id=ev.id,
                agent_id=agent.id,
                model_id="claude-sonnet-4",
                trigger_reason="manual",
                quality_score=51.94,
                performance_score=100.0,
                headline_details={
                    "quality_metric_keys": ["stix.entity_prf"],
                    "performance_metric_keys": ["performance.latency.wall_clock"],
                    "metric_entry_counts": {"stix.entity_prf": 2},
                    "quality_contributions": {"stix.entity_prf": 29.61},
                    "performance_contributions": {
                        "performance.latency.wall_clock": 100.0
                    },
                    "excluded": [
                        {"key": "stix.semantic_validity", "reason": "failed"}
                    ],
                    "performance_excluded": [
                        {
                            "key": "performance.tokens.total",
                            "reason": "not_available",
                        }
                    ],
                },
            )
            s.add(run)
            await s.commit()

            resp = await _build_run_response(s, run, missing_snapshots=[])

            # Each headline's composition stays attributed to that headline;
            # the run page groups by headline.
            assert resp["quality_metric_keys"] == ["stix.entity_prf"]
            assert resp["performance_metric_keys"] == [
                "performance.latency.wall_clock"
            ]

            # Both exclusion halves are present and NOT interchanged.
            assert resp["quality_aggregation_excluded"] == [
                {"key": "stix.semantic_validity", "reason": "failed"}
            ]
            assert resp["performance_aggregation_excluded"] == [
                {"key": "performance.tokens.total", "reason": "not_available"}
            ]

            # What each metric contributed, per headline. The Metrics panel's
            # raw means in native units do not reproduce either headline
            # (quality is penalised for inconsistency, performance normalised
            # across the evaluation), so these explain the difference.
            assert resp["quality_contributions"] == {"stix.entity_prf": 29.61}
            assert resp["performance_contributions"] == {
                "performance.latency.wall_clock": 100.0
            }
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_provenance_serialises_as_empty_not_null():
    """A historical run with no `headline_details` yields empty lists.

    The run page iterates these fields directly; `None` would crash any
    consumer that forgot to guard against it.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ev = Evaluation(name="Eval")
            agent = Agent(name="A")
            s.add_all([ev, agent])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=ev.id, agent_id=agent.id))
            run = EvaluationRun(
                evaluation_id=ev.id,
                agent_id=agent.id,
                model_id="claude-sonnet-4",
                trigger_reason="manual",
            )
            s.add(run)
            await s.commit()

            resp = await _build_run_response(s, run, missing_snapshots=[])

            assert resp["quality_aggregation_excluded"] == []
            assert resp["performance_aggregation_excluded"] == []
            assert resp["quality_metric_keys"] == []
            assert resp["performance_metric_keys"] == []
            assert resp["metric_entry_counts"] == {}
            assert resp["quality_contributions"] == {}
            assert resp["performance_contributions"] == {}
            # The scores themselves stay null — "not measured", never zero.
            assert resp["quality_score"] is None
            assert resp["performance_score"] is None
    finally:
        await cleanup()


_ABSTAINER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scores", "headline_details", "wire_field", "expected"),
    [
        # `not_a_default_contributor` survives serialisation; an unrecognised
        # reason passes through untouched, never normalised into a known one. The
        # headline itself is N/A, and the disclosure is what explains it.
        (
            {"quality_score": None},
            {
                "quality_metric_keys": [],
                "excluded": [
                    {"key": "prose.exact_match", "reason": "not_a_default_contributor"},
                    {"key": "prose.future", "reason": "reason_from_the_future"},
                ],
            },
            "quality_aggregation_excluded",
            [
                {"key": "prose.exact_match", "reason": "not_a_default_contributor"},
                {"key": "prose.future", "reason": "reason_from_the_future"},
            ],
        ),
        # A payload with only the original two reasons is served unchanged, in order.
        (
            {"quality_score": 42.0},
            {
                "quality_metric_keys": ["stix.entity_prf"],
                "excluded": [
                    {"key": "stix.semantic_validity", "reason": "failed"},
                    {"key": "stix.pattern_validity", "reason": "not_applicable"},
                ],
            },
            "quality_aggregation_excluded",
            [
                {"key": "stix.semantic_validity", "reason": "failed"},
                {"key": "stix.pattern_validity", "reason": "not_applicable"},
            ],
        ),
        # A run-level exclusion travels intact, `run_ids` included.
        (
            {"performance_score": None},
            {
                "performance_metric_keys": [],
                "performance_excluded": [
                    {
                        "key": "__run__",
                        "reason": "no_performance_data",
                        "run_ids": [_ABSTAINER_ID],
                    }
                ],
            },
            "performance_aggregation_excluded",
            [
                {
                    "key": "__run__",
                    "reason": "no_performance_data",
                    "run_ids": [_ABSTAINER_ID],
                }
            ],
        ),
        # The entries Overall Quality covers, and why the others are left out.
        (
            {"quality_score": 100.0},
            {"quality_coverage": {"entries": 2, "counted": 1, "left_out": {"reference_empty": 1}}},
            "quality_coverage",
            {"entries": 2, "counted": 1, "left_out": {"reference_empty": 1}},
        ),
    ],
    ids=[
        "default-omission-and-unknown-reason",
        "legacy-two-reasons",
        "run-level-with-run-ids",
        "quality-coverage",
    ],
)
async def test_a_stored_exclusion_is_served_verbatim_not_re_derived(
    scores, headline_details, wire_field, expected
):
    """Provenance is stored, never re-derived at read time: a serialiser that rebuilt
    it could report a composition the persisted value was not built from."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            ev = Evaluation(name="Eval")
            agent = Agent(name="A")
            s.add_all([ev, agent])
            await s.flush()
            s.add(EvaluationAgent(evaluation_id=ev.id, agent_id=agent.id))
            run = EvaluationRun(
                evaluation_id=ev.id,
                agent_id=agent.id,
                model_id="claude-sonnet-4",
                trigger_reason="manual",
                headline_details=headline_details,
                **scores,
            )
            s.add(run)
            await s.commit()

            resp = await _build_run_response(s, run, missing_snapshots=[])

        assert resp[wire_field] == expected
        assert {field: resp[field] for field in scores} == scores
    finally:
        await cleanup()


# ── Overall metric selections on evaluation responses ──
# The selections are tri-state (``None`` = inherit all defaults, ``[]`` = compose from
# nothing, ``[subset]`` = refine). A response missing either key falls back to ``None``,
# collapsing an explicit empty or partial selection to "inherit all".


_OVERALL_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    EvaluationDataset.__table__,
    EvaluationRun.__table__,
]


async def _make_overall_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("overallmetrics", tables=_OVERALL_TABLES))
    return db.sessions, stack.aclose


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_quality,stored_performance",
    [
        (None, None),
        ([], []),
        (["json.schema_validity"], ["latency_p95"]),
    ],
    ids=["inherit-all", "compose-from-nothing", "explicit-subset"],
)
async def test_response_round_trips_overall_metric_selection(
    stored_quality, stored_performance
):
    sm, cleanup = await _make_overall_db()
    try:
        async with sm() as s:
            admin = User(
                email="a@x.io", full_name="Admin", hashed_password="x", role="admin"
            )
            s.add(admin)
            await s.flush()
            ev = Evaluation(
                name="Eval",
                user_id=admin.id,
                overall_quality_metrics=stored_quality,
                overall_performance_metrics=stored_performance,
            )
            s.add(ev)
            await s.commit()

            resp = await evaluations_api._build_evaluation_response(s, ev, caller_can_manage=True)

            assert resp["overall_quality_metrics"] == stored_quality
            assert resp["overall_performance_metrics"] == stored_performance
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_evaluation_serves_whether_its_reader_may_manage_it():
    """``can_manage`` is ``helpers.can_manage()`` for the caller, checked as
    non-admins (rule 19): the owner and a ``config`` grantee may, a ``read_only``
    grantee may not."""
    async with (
        scratch_database("canmanage", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as s,
    ):
        owner, editor, reader = (
            User(email=f"{name}@x.io", full_name=name, hashed_password="x", role="user")
            for name in ("owner", "editor", "reader")
        )
        s.add_all([owner, editor, reader])
        await s.flush()
        ev = Evaluation(
            name="Shared",
            user_id=owner.id,
            shared_user_ids=[str(editor.id), str(reader.id)],
            shared_user_permissions={str(editor.id): "config", str(reader.id): "read_only"},
        )
        s.add(ev)
        await s.commit()

        served = {
            user.full_name: (await evaluations_api.get_evaluation(str(ev.id), user=user, db=s))["can_manage"]
            for user in (owner, editor, reader)
        }

    assert served == {"owner": True, "editor": True, "reader": False}


# ── How a metric reads on one entry ──
# The classification F1s are three ways of combining entries. On one entry, the pooled one
# is the entry's own label F1, the per-entry one repeats it and the per-label one is no
# longer a macro F1, so the entry's Metrics strip shows one "Label F1". Every other metric
# reads on an entry as it always did.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_entry_serves_one_label_f1_and_every_other_metric_as_before():
    values = {
        "classification.micro_prf": 83.33,
        "classification.macro_prf": 71.43,
        "classification.samples_f1": 83.33,
        "classification.subset_accuracy": 0.0,
        "stix.entity_prf": 50.0,
        "performance.latency.wall_clock": 1200.0,
    }
    async with contextlib.AsyncExitStack() as stack:
        db = await stack.enter_async_context(
            scratch_database("entryview", tables=ALL_TABLES, extensions=("vector",))
        )
        async with db.sessions() as session:
            agent = Agent(name="Classifier")
            evaluation = Evaluation(name="Eval")
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name="Admin",
                role="admin",
            )
            session.add_all([agent, evaluation, admin])
            await session.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4o",
                trigger_reason="manual",
                status="completed",
            )
            session.add(run)
            await session.flush()
            result = EvaluationResult(run_id=run.id, agent_output="[]", score=80.0)
            session.add(result)
            await session.flush()
            for key, value in values.items():
                session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key=key, value=value))
            await session.commit()

            rows = await get_result_metrics(str(run.id), str(result.id), user=admin, db=session)

    served = {
        row["key"]: (row["value"], row["shown_on_entry"], row["entry_label"])
        for row in (MetricResponse.model_validate(r).model_dump() for r in rows)
    }
    assert served == {
        "classification.micro_prf": (83.33, True, "Label F1"),
        "classification.macro_prf": (71.43, False, None),
        "classification.samples_f1": (83.33, False, None),
        "classification.subset_accuracy": (0.0, True, None),
        "stix.entity_prf": (50.0, True, None),
        "performance.latency.wall_clock": (1200.0, True, None),
    }
