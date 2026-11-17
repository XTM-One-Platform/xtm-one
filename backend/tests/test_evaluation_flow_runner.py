"""Running a flow on one evaluation entry (#8257).

``services/evaluation/flow_subject.run_flow_for_entry`` starts a manual run of
the flow from the entry, waits for it, and answers its final output - the one a
``flow.call`` parent reads - or ``Error: ...`` when the run did not complete.
The walker is not under test: a stand-in finishes the run the way it would.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.models.flow_run import FlowRun, FlowRunStep
from app.services.evaluation import flow_subject
from app.services.evaluation.flow_subject import (
    EVALUATION_TRIGGER_SOURCE,
    FlowSubjectUnavailableError,
    entry_parameters,
    run_flow_for_entry,
)
from app.services.flow_runner import TriggerParametersError, manual_run_trigger
from app.services.run_ledger import calls_from_spans, ledger_from_trace

MANUAL = {"id": "start", "kind": "trigger.manual", "config": {}}
TYPED = {
    "id": "start",
    "kind": "trigger.manual",
    "config": {"parameters": [{"name": "count", "type": "number", "required": True}]},
}
CHAT = {"id": "chat", "kind": "trigger.chat", "config": {}}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"count": 3}', {"count": 3}),
        ("[1, 2]", {"prompt": "[1, 2]", "message": "[1, 2]"}),
        ("Summarise the incident", {"prompt": "Summarise the incident", "message": "Summarise the incident"}),
        ("", {"prompt": "", "message": ""}),
        (None, {"prompt": "", "message": ""}),
    ],
    ids=["json-object", "json-list", "text", "empty", "none"],
)
def test_an_entry_becomes_the_runs_parameters(text, expected):
    assert entry_parameters(text) == expected


def test_a_manual_trigger_takes_the_prompt():
    node, trigger_type, payload = manual_run_trigger({"nodes": [CHAT, MANUAL]}, {"prompt": "hi", "message": "hi"})

    assert (node["id"], trigger_type) == ("start", "manual")
    assert payload == {"payload": {"prompt": "hi", "message": "hi"}, "prompt": "hi"}


def test_a_chat_trigger_takes_the_message():
    _node, trigger_type, payload = manual_run_trigger({"nodes": [CHAT]}, {"prompt": "hi", "message": "hi"})

    assert (trigger_type, payload) == ("chat", {"message": "hi"})


def test_declared_parameters_are_coerced():
    _node, _type, payload = manual_run_trigger({"nodes": [TYPED]}, {"count": "3"})

    assert payload == {"payload": {"count": 3}, "prompt": ""}


def test_parameters_that_do_not_match_the_declaration_are_refused():
    with pytest.raises(TriggerParametersError) as refused:
        manual_run_trigger({"nodes": [TYPED]}, {"prompt": "no count here"})

    assert len(refused.value.errors) == 1


def test_a_graph_without_a_trigger_is_refused():
    with pytest.raises(LookupError):
        manual_run_trigger({"nodes": [{"id": "x", "kind": "llm.prompt"}]}, {})


def test_stored_spans_give_the_ledger_live_spans_give():
    spans = [
        {
            "type": "GENERATION",
            "name": "gen",
            "model": "m1",
            "duration_ms": 12.0,
            "level": "DEFAULT",
            "usage_details": {"input": 3, "output": 4},
        },
        {"type": "TOOL", "name": "tool:search", "duration_ms": 5.0, "level": "ERROR", "status_message": "TimeoutError: x"},
        {"type": "SPAN", "name": "iteration"},
    ]

    assert calls_from_spans(spans) == ledger_from_trace({"local_spans": spans})["calls"]
    assert [(c["kind"], c["name"], c["outcome"]) for c in calls_from_spans(spans)] == [
        ("model", "gen", "success"),
        ("tool", "search", "timeout"),
    ]


# --- PostgreSQL --------------------------------------------------------------


async def _world(db, graph: dict):
    from app.models.flow import Flow
    from app.models.user import User

    async with db.sessions() as s:
        owner = User(email=f"o-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner")
        s.add(owner)
        await s.flush()
        flow = Flow(user_id=owner.id, name=f"f-{uuid.uuid4().hex[:6]}", graph=graph)
        s.add(flow)
        await s.commit()
        return owner.id, flow.id


async def _finish(db, *, status: str, output: str | None = None, error: str | None = None) -> None:
    """The walker's part: the run's step journal and terminal status."""
    while True:
        async with db.sessions() as s:
            run = (await s.execute(select(FlowRun))).scalars().first()
            if run is not None:
                now = datetime.now(UTC)
                run.started_at = run.started_at or now
                if output is not None:
                    s.add(
                        FlowRunStep(
                            flow_run_id=run.id,
                            seq=1,
                            node_id="answer",
                            node_kind="custom.unknown",
                            status="completed",
                            output={"text": output},
                            started_at=now,
                            completed_at=now,
                        )
                    )
                run.status = status
                run.error = error
                run.completed_at = now
                await s.commit()
                return
        await asyncio.sleep(0.05)


async def _run(db, flow_id, owner_id, *, timeout: float = 30, cancelled: bool = False, text: str = "Go"):
    from app.models.flow import Flow

    async def _is_cancelled() -> bool:
        return cancelled

    async with db.sessions() as s:
        flow = await s.get(Flow, flow_id)
        return await run_flow_for_entry(
            s,
            flow,
            evaluation_run_id=uuid.UUID(int=1),
            entry_id=uuid.UUID(int=2),
            input_prompt=text,
            owner_id=owner_id,
            timeout_seconds=timeout,
            is_cancelled=_is_cancelled,
        )


@pytest.fixture
def quick(monkeypatch):
    from app.services import orchestration_engine

    async def _no_wake() -> None:
        return None

    monkeypatch.setattr(flow_subject, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(flow_subject, "_TRACE_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(orchestration_engine, "notify_orchestration_wake", _no_wake)


@pytest.mark.integration
@pytest.mark.usefixtures("quick")
async def test_a_completed_run_answers_its_final_output_uncut():
    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from tests.scratch_database import ALL_TABLES, scratch_database

    long_answer = "A" * 2500
    async with scratch_database("evalflowrun", tables=ALL_TABLES, extensions=("vector",)) as db:
        owner_id, flow_id = await _world(db, {"nodes": [MANUAL]})
        outcome, _ = await asyncio.gather(
            _run(db, flow_id, owner_id, text="Summarise"), _finish(db, status="completed", output=long_answer)
        )
        async with db.sessions() as s:
            run = (await s.execute(select(FlowRun))).scalar_one()

    assert (outcome.status, outcome.output, outcome.cancelled) == ("completed", long_answer, False)
    assert outcome.flow_run_id == run.id
    assert (run.trigger_type, run.user_id) == ("manual", owner_id)
    assert run.trigger_payload == {"payload": {"prompt": "Summarise", "message": "Summarise"}, "prompt": "Summarise"}
    assert run.trigger_context == {
        "source": EVALUATION_TRIGGER_SOURCE,
        "evaluation_run_id": str(uuid.UUID(int=1)),
        "evaluation_entry_id": str(uuid.UUID(int=2)),
    }
    assert [step["node_id"] for step in outcome.timeline] == ["answer"]
    # No trace landed: the totals are unavailable, never a measured zero.
    assert (outcome.trace_ids, outcome.ledger) == ([], None)


@pytest.mark.integration
@pytest.mark.usefixtures("quick")
async def test_a_failed_run_answers_an_error_the_judge_scores():
    import app.models  # noqa: F401
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("evalflowfail", tables=ALL_TABLES, extensions=("vector",)) as db:
        owner_id, flow_id = await _world(db, {"nodes": [MANUAL]})
        outcome, _ = await asyncio.gather(_run(db, flow_id, owner_id), _finish(db, status="failed", error="boom"))

    assert (outcome.status, outcome.output) == ("failed", "Error: the flow run ended failed: boom")


@pytest.mark.integration
@pytest.mark.usefixtures("quick")
@pytest.mark.parametrize(
    ("timeout", "cancelled", "status", "output"),
    [
        (0.0, False, "timed_out", "Error: the flow run did not finish within 0 s."),
        (30.0, True, "cancelled", ""),
    ],
    ids=["past-the-bound", "evaluation-cancelled"],
)
async def test_a_run_that_does_not_end_is_stopped(timeout, cancelled, status, output):
    import app.models  # noqa: F401
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("evalflowstop", tables=ALL_TABLES, extensions=("vector",)) as db:
        owner_id, flow_id = await _world(db, {"nodes": [MANUAL]})
        outcome = await _run(db, flow_id, owner_id, timeout=timeout, cancelled=cancelled)
        async with db.sessions() as s:
            run = (await s.execute(select(FlowRun))).scalar_one()

    assert (outcome.status, outcome.output, outcome.cancelled) == (status, output, cancelled)
    assert run.status == "cancelled"
    assert run.completed_at is not None


@pytest.mark.integration
@pytest.mark.usefixtures("quick")
async def test_an_entry_that_does_not_fit_the_trigger_starts_no_run():
    import app.models  # noqa: F401
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("evalflowparams", tables=ALL_TABLES, extensions=("vector",)) as db:
        owner_id, flow_id = await _world(db, {"nodes": [TYPED]})
        with pytest.raises(FlowSubjectUnavailableError) as refused:
            await _run(db, flow_id, owner_id, text=json.dumps({"other": 1}))
        async with db.sessions() as s:
            runs = await s.scalar(select(func.count()).select_from(FlowRun))

    assert str(refused.value).startswith("The entry does not match the flow's run parameters: ")
    assert runs == 0
