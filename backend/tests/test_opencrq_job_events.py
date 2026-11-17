"""OpenCRQ job-run events for integration_event assignments (#8991, CTEM S2d)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import event_poller, opencrq_job_events


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value


def _wf(filter_config: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_config={"provider": "opencrq", "event": "risk_scenario_recomputed", "filter": filter_config or {}},
    )


def _run(run_id: str, finished: str, kind: str = "crq-simulation") -> dict:
    return {
        "id": run_id,
        "kind": kind,
        "status": "succeeded",
        "enqueuedAt": finished,
        "startedAt": finished,
        "finishedAt": finished,
        "processedTotal": 3,
        "expectedTotal": 3,
        "triggeredBy": "schedule",
        "attempt": 1,
    }


def test_catalog_declares_the_three_events_as_polling():
    events = {e["event"]: e for e in PROVIDER_CATALOG["opencrq"]["available_events"]}
    assert set(events) == set(opencrq_job_events.EVENTS)
    assert {e["mechanism"] for e in events.values()} == {"polling"}


def test_every_event_is_polled_every_minute():
    for event in opencrq_job_events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == opencrq_job_events.POLL_INTERVAL_SECONDS == 60


def test_runs_query_asks_succeeded_runs_of_the_event_kinds_oldest_first():
    query = opencrq_job_events.runs_query("control_validation_ingested", "2026-11-06T07:00:00Z", 51)
    assert query == {
        "kinds": ["openaev-findings-sync", "proven-posture-record"],
        "statuses": ["succeeded"],
        "finishedFrom": "2026-11-06T07:00:00Z",
        "sortBy": "finishedAt",
        "sortDesc": False,
        "limit": 51,
    }


def test_run_event_carries_the_run_and_a_stable_id():
    payload = opencrq_job_events.run_event(
        _run("r1", "2026-11-06T07:01:00Z"), "risk_scenario_recomputed", base_url="https://crq", tenant_id="t1"
    )
    assert payload["message_id"] == "opencrq:t1:r1"
    assert payload["event"] == "risk_scenario_recomputed"
    assert payload["entity"]["kind"] == "crq-simulation"
    assert payload["entity"]["finished_at"] == "2026-11-06T07:01:00Z"
    assert payload["entity"]["tenant_id"] == "t1"
    assert payload["entity"]["instance_url"] == "https://crq"


def test_a_run_without_id_has_no_message_id():
    payload = opencrq_job_events.run_event({"kind": "x"}, "finding_score_changed", base_url="", tenant_id="t")
    assert payload["message_id"] == ""


@pytest.mark.asyncio
async def test_list_finished_runs_refuses_a_credential_without_base_url():
    runs, _, _, err = await opencrq_job_events.list_finished_runs("token-only", "finding_score_changed", "x", limit=5)
    assert runs == [] and err


@pytest.mark.asyncio
async def test_list_finished_runs_reads_jobs_list_all_runs(monkeypatch):
    from app.integrations.providers.opencrq import _common

    calls = []

    async def _resolve(base_url, headers, tenant_id, **_):
        return tenant_id or "sole", None

    async def _trpc(base_url, headers, procedure, action, **kwargs):
        calls.append((base_url, procedure, kwargs))
        return {"items": [_run("a", "2026-11-06T07:01:00Z"), {"id": "b", "finishedAt": None}], "total": 2}, None

    monkeypatch.setattr(_common, "_resolve_tenant", _resolve)
    monkeypatch.setattr(_common, "_trpc", _trpc)
    runs, base_url, tenant, err = await opencrq_job_events.list_finished_runs(
        "https://crq/||tok", "finding_score_changed", "2026-11-06T07:00:00Z", limit=51
    )
    assert err is None and tenant == "sole" and base_url == "https://crq"
    assert [r["id"] for r in runs] == ["a"]
    assert calls[0][1] == "jobs.listAllRuns"
    assert calls[0][2]["tenant_id"] == "sole"
    assert calls[0][2]["input"]["kinds"] == ["finding-score-recompute"]


@pytest.mark.asyncio
async def test_first_poll_starts_from_now():
    redis = _Redis()
    wf = _wf()
    events = await event_poller._poll_opencrq_job_runs("https://crq||tok", "risk_scenario_recomputed", wf, redis)
    assert events == []
    assert redis.values[f"event_cursor:{wf.id}"]


@pytest.mark.asyncio
async def test_poll_dispatches_oldest_first_and_moves_the_cursor(monkeypatch):
    wf = _wf({"tenant_id": "t9"})
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-06T07:00:00Z"})
    seen = {}

    async def _list(credential, event_type, since, *, tenant_id="", limit):
        seen.update(since=since, tenant_id=tenant_id, limit=limit)
        return (
            [_run("late", "2026-11-06T07:05:00Z"), _run("early", "2026-11-06T07:02:00Z")],
            "https://crq",
            tenant_id,
            None,
        )

    monkeypatch.setattr(opencrq_job_events, "list_finished_runs", _list)
    events = await event_poller._poll_opencrq_job_runs("https://crq||tok", "risk_scenario_recomputed", wf, redis)
    assert seen == {"since": "2026-11-06T07:00:00Z", "tenant_id": "t9", "limit": event_poller._OPENCRQ_PAGE + 1}
    assert [e["message_id"] for e in events] == ["opencrq:t9:early", "opencrq:t9:late"]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-06T07:05:00Z"


@pytest.mark.asyncio
async def test_a_failed_poll_keeps_the_cursor(monkeypatch):
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": "2026-11-06T07:00:00Z"})

    async def _list(*_, **__):
        return [], "", "", "Error: OpenCRQ 503"

    monkeypatch.setattr(opencrq_job_events, "list_finished_runs", _list)
    assert await event_poller._poll_opencrq_job_runs("https://crq||tok", "finding_score_changed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-06T07:00:00Z"


@pytest.mark.asyncio
async def test_poll_provider_routes_opencrq_events(monkeypatch):
    called = {}

    async def _poll(credential, event_type, wf, redis):
        called["event"] = event_type
        return []

    monkeypatch.setattr(event_poller, "_poll_opencrq_job_runs", _poll)
    await event_poller._poll_provider("opencrq", "control_validation_ingested", "c", _wf(), _Redis(), "k")
    assert called == {"event": "control_validation_ingested"}


@pytest.mark.asyncio
async def test_poll_credential_is_the_tool_compound(monkeypatch):
    from app.services import integration_tools

    async def _get_token(db, integration_id, provider, **kwargs):
        assert provider == "opencrq" and kwargs["user_id"] == "u1"
        return "https://crq||tok"

    monkeypatch.setattr(integration_tools, "_get_token", _get_token)
    integration = SimpleNamespace(id=uuid.uuid4())
    token = await event_poller._poll_credential("opencrq", integration, integration, db=object(), user_id="u1")
    assert token == "https://crq||tok"
