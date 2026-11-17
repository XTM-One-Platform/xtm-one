"""A Webhook trigger's concurrent runs, through the real route and PostgreSQL.

``POST /api/hooks/flows/{flow_id}/{path}`` end to end - the secret, the dedup
slot, the admission (advisory lock + active-run count) and the 429 - against a
scratch database. Only the agentic gate is stubbed (it reads the licence on the
app's own engine). Runs stay ``pending``: no worker walks them, so each one
created stays active for the rest of the test.
"""

import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from starlette.requests import Request

from app.models.flow_run import FLOW_RUN_ACTIVE_STATUSES, FlowRun
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_flow_runner_postgres import _seed, _temp_database

pytestmark = pytest.mark.integration

_SECRET = "s3cret-for-the-webhook-test"
_PATH = "alerts"


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


def _graph(**trigger_config):
    return {
        "nodes": [
            {"id": "t1", "kind": "trigger.webhook", "position": {"x": 0, "y": 0},
             "config": {"path": _PATH, **trigger_config}},
            {"id": "s1", "kind": "util.stop", "position": {"x": 0, "y": 0},
             "config": {"result": "ok"}},
        ],
        "edges": [
            {"id": "e1", "source": "t1", "source_handle": "out", "target": "s1",
             "target_handle": "in", "kind": "data"},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


async def _active_flow(session, **trigger_config):
    user, flow = await _seed(session, graph=_graph(**trigger_config))
    flow.status = "active"
    flow.webhook_secret = _SECRET
    await session.commit()
    return user, flow


def _request(body: dict) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/hooks/flows",
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("203.0.113.9", 4242),
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-webhook-token", _SECRET.encode()),
            ],
        },
        receive,
    )


async def _deliver(flow, alert: str) -> int:
    """One delivery through the route: 200 or the status it raised."""
    from app.api.v1 import webhooks

    try:
        answer = await webhooks.receive_flow_webhook(
            str(flow.id), _PATH, _request({"alert": alert, "nonce": uuid.uuid4().hex})
        )
    except HTTPException as exc:
        assert exc.status_code == 429
        assert exc.headers == {"Retry-After": "30"}
        return 429
    assert answer["status"] == "ok", answer
    return 200


async def _active_runs(session, flow) -> int:
    return (
        await session.execute(
            select(func.count(FlowRun.id)).where(
                FlowRun.flow_id == flow.id,
                FlowRun.status.in_(FLOW_RUN_ACTIVE_STATUSES),
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_by_default_a_delivery_waits_for_the_active_run():
    async with _temp_database() as sessions, sessions() as session:
        _, flow = await _active_flow(session)

        assert [await _deliver(flow, f"a{i}") for i in range(2)] == [200, 429]
        assert await _active_runs(session, flow) == 1


@pytest.mark.asyncio
async def test_deliveries_start_runs_up_to_the_trigger_s_limit():
    async with _temp_database() as sessions, sessions() as session:
        _, flow = await _active_flow(session, max_concurrent_runs=3)

        assert [await _deliver(flow, f"a{i}") for i in range(4)] == [200, 200, 200, 429]
        assert await _active_runs(session, flow) == 3


@pytest.mark.asyncio
async def test_simultaneous_deliveries_never_exceed_the_limit_together():
    """The admission lock: counted one after the other, not all against zero."""
    async with _temp_database() as sessions, sessions() as session:
        _, flow = await _active_flow(session, max_concurrent_runs=2)

        statuses = await asyncio.gather(*(_deliver(flow, f"a{i}") for i in range(6)))

        assert sorted(statuses) == [200, 200, 429, 429, 429, 429]
        assert await _active_runs(session, flow) == 2


@pytest.mark.asyncio
async def test_a_run_started_by_another_trigger_counts_against_the_limit():
    from app.services.flow_runner import create_flow_run

    async with _temp_database() as sessions, sessions() as session:
        user, flow = await _active_flow(session, max_concurrent_runs=2)
        await create_flow_run(
            session, flow, trigger_type="manual", trigger_payload={},
            trigger_context={"type": "manual"}, user_id=user.id,
        )
        await session.commit()

        assert [await _deliver(flow, f"a{i}") for i in range(2)] == [200, 429]
        assert await _active_runs(session, flow) == 2


@pytest.mark.asyncio
async def test_a_refused_delivery_releases_the_admission_lock():
    """After a 429 the next delivery is admitted, not stuck behind the lock."""
    async with _temp_database() as sessions, sessions() as session:
        _, flow = await _active_flow(session)
        assert [await _deliver(flow, f"a{i}") for i in range(2)] == [200, 429]
        run = (
            await session.execute(select(FlowRun).where(FlowRun.flow_id == flow.id))
        ).scalar_one()
        run.status = "completed"
        await session.commit()

        assert await asyncio.wait_for(_deliver(flow, "a2"), timeout=10) == 200
        assert await _active_runs(session, flow) == 1


@pytest.mark.asyncio
async def test_a_finished_run_frees_its_place():
    async with _temp_database() as sessions, sessions() as session:
        _, flow = await _active_flow(session)
        assert await _deliver(flow, "a0") == 200
        run = (
            await session.execute(select(FlowRun).where(FlowRun.flow_id == flow.id))
        ).scalar_one()
        run.status = "completed"
        await session.commit()

        assert await _deliver(flow, "a1") == 200
        assert await _active_runs(session, flow) == 1
