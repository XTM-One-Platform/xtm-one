"""OpenCRQ job-run events for integration_event assignments (#8991, CTEM S2d)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import event_coalescing, event_poller, opencrq_job_events


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def zscore(self, key: str, member: str) -> float | None:
        score: float | None = self.values.get(key, {}).get(member)
        return score


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
    # The page is one event: the newest run, covering both (#1417).
    assert [e["message_id"] for e in events] == ["opencrq:t9:late"]
    assert [c["message_id"] for c in events[0]["covers"]] == ["opencrq:t9:early", "opencrq:t9:late"]
    assert events[0]["run_count"] == 2
    assert events[0]["coalesce_key"] == "opencrq:t9:risk_scenario_recomputed"
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-11-06T07:05:00Z"


def _burst(count: int) -> list[dict[str, Any]]:
    return [_run(f"r{i:02d}", f"2026-11-06T07:{i:02d}:00Z") for i in range(count)]


def test_a_batch_recompute_of_thirty_jobs_is_one_event() -> None:
    events = opencrq_job_events.coalesce_events(
        _burst(30), "risk_scenario_recomputed", base_url="https://crq", tenant_id="t"
    )
    assert len(events) == 1
    assert events[0]["run_count"] == 30
    assert events[0]["message_id"] == "opencrq:t:r29"
    assert len(events[0]["covers"]) == 30


def test_a_single_job_is_still_one_event() -> None:
    events = opencrq_job_events.coalesce_events(
        _burst(1), "risk_scenario_recomputed", base_url="https://crq", tenant_id="t"
    )
    assert [e["message_id"] for e in events] == ["opencrq:t:r00"]
    assert events[0]["run_count"] == 1


def test_a_job_that_processed_nothing_is_no_event() -> None:
    noop = {**_run("n", "2026-11-06T07:00:00Z"), "processedTotal": 0}
    assert opencrq_job_events.coalesce_events([noop], "finding_score_changed", base_url="", tenant_id="t") == []
    unknown = {**_run("u", "2026-11-06T07:00:00Z"), "processedTotal": None}
    assert len(opencrq_job_events.coalesce_events([unknown], "finding_score_changed", base_url="", tenant_id="t")) == 1


def _score_run(run_id: str, changed: int | None) -> dict[str, Any]:
    run = {
        **_run(run_id, "2026-11-25T13:00:00Z", kind="finding-score-recompute"),
        "processedTotal": 40,
        "expectedTotal": 40,
    }
    if changed is not None:
        summary = {
            "scored": 40,
            "changedFindings": changed,
            "changedPostures": 0,
            "changedUniqueFindings": 0,
            "changedCount": changed,
        }
        run["currentStep"] = json.dumps(summary)
    return run


def test_a_finding_score_run_that_changed_nothing_is_no_event() -> None:
    """OpenCRQ #232 reports changedCount: a recompute that moved no score is dropped (#2169)."""
    still = _score_run("s0", 0)
    assert opencrq_job_events.coalesce_events([still], "finding_score_changed", base_url="", tenant_id="t") == []
    # An OpenCRQ that does not report it yet keeps every run, as before.
    (older,) = opencrq_job_events.coalesce_events(
        [_score_run("s1", None)], "finding_score_changed", base_url="", tenant_id="t"
    )
    assert "changed_count" not in older and "changed_count" not in older["entity"]


def test_the_changed_count_adds_up_over_a_page_and_is_a_filter_key() -> None:
    runs = [_score_run("s1", 3), _score_run("s2", 0), _score_run("s3", 2)]
    (event,) = opencrq_job_events.coalesce_events(runs, "finding_score_changed", base_url="", tenant_id="t")
    assert [c["id"] for c in event["covers"]] == ["s1", "s3"]
    assert event["changed_count"] == event["entity"]["changed_count"] == 5
    assert [c["changed_count"] for c in event["covers"]] == [3, 2]
    assert "changed_count" in opencrq_job_events.FILTER_KEYS
    (later,) = opencrq_job_events.coalesce_events(
        [_score_run("s4", 1)], "finding_score_changed", base_url="", tenant_id="t"
    )
    merged = event_coalescing.merged_payload(event, later)
    assert merged["changed_count"] == merged["entity"]["changed_count"] == 6


def test_a_kind_that_never_reports_progress_is_kept_at_zero() -> None:
    """A job that reports no expected total ends at 0 whatever it did: its run is still an event."""
    silent = {
        **_run("p", "2026-11-06T07:00:00Z", kind="crq-price-recompute"),
        "processedTotal": 0,
        "expectedTotal": None,
    }
    events = opencrq_job_events.coalesce_events([silent], "risk_scenario_recomputed", base_url="", tenant_id="t")
    assert [e["message_id"] for e in events] == ["opencrq:t:p"]


def test_the_job_payload_scenario_ids_travel_on_the_event() -> None:
    one = {**_run("a", "2026-11-06T07:00:00Z"), "payload": {"riskScenarioId": "s1"}}
    two = {**_run("b", "2026-11-06T07:01:00Z"), "payload": {"riskScenarioIds": ["s2", "s1"]}}
    plain = _run("c", "2026-11-06T07:02:00Z")
    (event,) = opencrq_job_events.coalesce_events(
        [one, two, plain], "risk_scenario_recomputed", base_url="", tenant_id="t"
    )
    assert event["entity"]["risk_scenario_ids"] == ["s1", "s2"]
    assert [c["risk_scenario_ids"] for c in event["covers"]] == [["s1"], ["s2", "s1"], []]
    single = opencrq_job_events.run_event(one, "risk_scenario_recomputed", base_url="", tenant_id="t")
    assert single["entity"]["risk_scenario_ids"] == ["s1"]
    assert "risk_scenario_ids" not in opencrq_job_events.run_event(plain, "x", base_url="", tenant_id="t")["entity"]


def _priced(run_id: str, finished: str, summary: dict[str, Any], key: str = "currentStep") -> dict[str, Any]:
    import json

    return {**_run(run_id, finished, kind="crq-price-recompute"), key: json.dumps(summary)}


def test_the_moved_scenarios_of_a_price_pass_travel_on_the_event() -> None:
    """Today's OpenCRQ summary (moved, movedCount; no movedAle) read from currentStep (#1663)."""
    run = _priced("p", "2026-11-06T07:00:00Z", {"walked": 3, "priced": 2, "moved": ["s1", "s2"], "movedCount": 2})
    payload = opencrq_job_events.run_event(run, "risk_scenario_recomputed", base_url="", tenant_id="t")
    for target in (payload, payload["entity"]):
        assert target["moved_scenario_ids"] == ["s1", "s2"]
        assert target["moved_count"] == 2
        assert "moved_ale" not in target
    assert payload["entity"]["progress_message"] == run["currentStep"]
    assert payload["tenant_id"] == "t" and payload["kind"] == "crq-price-recompute"


def test_the_legacy_progress_message_key_is_still_read() -> None:
    run = _priced("p", "2026-11-06T07:00:00Z", {"moved": ["s1"]}, key="progressMessage")
    payload = opencrq_job_events.run_event(run, "risk_scenario_recomputed", base_url="", tenant_id="t")
    assert payload["moved_scenario_ids"] == ["s1"] and payload["moved_count"] == 1


def test_a_run_without_a_json_summary_moved_nothing() -> None:
    plain = {**_run("a", "2026-11-06T07:00:00Z"), "currentStep": "Simulating 3 scenarios"}
    payload = opencrq_job_events.run_event(plain, "risk_scenario_recomputed", base_url="", tenant_id="t")
    assert payload["moved_scenario_ids"] == [] and payload["moved_count"] == 0
    broken = {**_run("b", "2026-11-06T07:00:00Z"), "currentStep": "{not json"}
    assert opencrq_job_events.run_event(broken, "x", base_url="", tenant_id="t")["moved_scenario_ids"] == []


def test_moved_ale_travels_when_opencrq_reports_it() -> None:
    summary = {
        "moved": ["s1", "s2"],
        "movedCount": 140,
        "movedAle": [
            {"riskScenarioId": "s1", "aleBefore": 100, "aleAfter": 150.5, "aleDelta": 50.5},
            {"riskScenarioId": "s2", "aleBefore": None, "aleAfter": 10, "aleDelta": None},
            {"aleBefore": 1},
        ],
    }
    payload = opencrq_job_events.run_event(
        _priced("p", "2026-11-06T07:00:00Z", summary), "risk_scenario_recomputed", base_url="", tenant_id="t"
    )
    assert payload["moved_count"] == 140
    assert payload["moved_ale"] == [
        {"risk_scenario_id": "s1", "ale_before": 100.0, "ale_after": 150.5, "ale_delta": 50.5},
        {"risk_scenario_id": "s2", "ale_before": None, "ale_after": 10.0, "ale_delta": None},
    ]


def test_a_coalesced_page_merges_the_moved_scenarios() -> None:
    first = _priced(
        "a",
        "2026-11-06T07:00:00Z",
        {
            "moved": ["s1"],
            "movedCount": 1,
            "movedAle": [{"riskScenarioId": "s1", "aleBefore": 1, "aleAfter": 2, "aleDelta": 1}],
        },
    )
    second = _priced(
        "b",
        "2026-11-06T07:01:00Z",
        {
            "moved": ["s1", "s2"],
            "movedCount": 2,
            "movedAle": [{"riskScenarioId": "s1", "aleBefore": 2, "aleAfter": 3, "aleDelta": 1}],
        },
    )
    simulation = {**_run("c", "2026-11-06T07:02:00Z"), "payload": {"riskScenarioId": "s9"}}
    (event,) = opencrq_job_events.coalesce_events(
        [first, second, simulation], "risk_scenario_recomputed", base_url="", tenant_id="t"
    )
    assert event["moved_scenario_ids"] == event["entity"]["moved_scenario_ids"] == ["s1", "s2"]
    # Distinct scenarios, never the per-run sum (1 + 2) (#2117).
    assert event["moved_count"] == event["entity"]["moved_count"] == 2
    assert event["moved_ale"] == [{"risk_scenario_id": "s1", "ale_before": 2.0, "ale_after": 3.0, "ale_delta": 1.0}]
    assert event["kind"] == ["crq-price-recompute", "crq-simulation"]
    assert event["risk_scenario_ids"] == ["s9"]
    assert [c["moved_scenario_ids"] for c in event["covers"]] == [["s1"], ["s1", "s2"], []]


def test_a_capped_run_keeps_its_own_larger_count() -> None:
    capped = _priced("a", "2026-11-06T07:00:00Z", {"moved": ["s1"], "movedCount": 40})
    other = _priced("b", "2026-11-06T07:01:00Z", {"moved": ["s1", "s2"], "movedCount": 2})
    (event,) = opencrq_job_events.coalesce_events(
        [capped, other], "risk_scenario_recomputed", base_url="", tenant_id="t"
    )
    assert event["moved_count"] == 40


def test_a_page_folded_into_a_waiting_run_unions_its_moved_scenarios() -> None:
    from app.services import event_coalescing

    def page(run_id: str, at: str, moved: list[str], delta: float) -> dict:
        run = _priced(
            run_id,
            at,
            {
                "moved": moved,
                "movedCount": len(moved),
                "movedAle": [
                    {"riskScenarioId": sid, "aleBefore": 1, "aleAfter": 1 + delta, "aleDelta": delta} for sid in moved
                ],
            },
        )
        (event,) = opencrq_job_events.coalesce_events([run], "risk_scenario_recomputed", base_url="", tenant_id="t")
        return event

    waiting = page("a", "2026-11-06T07:00:00Z", ["s1", "s2"], 1.0)
    incoming = page("b", "2026-11-06T07:01:00Z", ["s2", "s3"], 2.0)
    merged = event_coalescing.merged_payload(waiting, incoming)
    assert merged["moved_scenario_ids"] == merged["entity"]["moved_scenario_ids"] == ["s1", "s2", "s3"]
    assert merged["moved_count"] == merged["entity"]["moved_count"] == 3
    deltas = {m["risk_scenario_id"]: m["ale_delta"] for m in merged["moved_ale"]}
    assert deltas == {"s1": 1.0, "s2": 2.0, "s3": 2.0}
    assert merged["entity"]["id"] == "b"
    assert merged["run_count"] == 2


def test_the_trigger_filter_reacts_to_a_moved_scenario() -> None:
    from app.services.integration_event_filters import event_filter_reason

    run = _priced("p", "2026-11-06T07:00:00Z", {"moved": ["s1", "s2"], "movedCount": 2})
    (event,) = opencrq_job_events.coalesce_events([run], "risk_scenario_recomputed", base_url="", tenant_id="t9")
    assert event_filter_reason("risk_scenario_recomputed", event, {"moved_scenario_ids": "s2"}) is None
    assert event_filter_reason("risk_scenario_recomputed", event, {"moved_scenario_ids": ["s7", "s1"]}) is None
    assert event_filter_reason("risk_scenario_recomputed", event, {"moved_scenario_ids": "s7"}) is not None
    assert event_filter_reason("risk_scenario_recomputed", event, {"moved_count": {"ne": 0}}) is None
    assert event_filter_reason("risk_scenario_recomputed", event, {"kind": "crq-price-recompute"}) is None
    # The tenant the poller reads is also a payload field: the filter keeps its events.
    assert event_filter_reason("risk_scenario_recomputed", event, {"tenant_id": "t9"}) is None


def test_the_catalog_declares_the_filter_keys_of_every_event() -> None:
    for entry in PROVIDER_CATALOG["opencrq"]["available_events"]:
        assert set(entry["filter_keys"]) == set(opencrq_job_events.FILTER_KEYS)
    assert {"moved_scenario_ids", "tenant_id"} <= set(opencrq_job_events.FILTER_KEYS)


def test_the_coalesce_key_is_per_tenant_and_event() -> None:
    assert opencrq_job_events.coalesce_key("risk_scenario_recomputed", "a") != opencrq_job_events.coalesce_key(
        "risk_scenario_recomputed", "b"
    )
    assert opencrq_job_events.coalesce_key("risk_scenario_recomputed", "a") != opencrq_job_events.coalesce_key(
        "finding_score_changed", "a"
    )


@pytest.mark.asyncio
async def test_runs_already_dispatched_are_left_out_of_a_page_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    wf = _wf({"tenant_id": "t9"})
    redis = _Redis(
        {
            f"event_cursor:{wf.id}": "2026-11-06T07:00:00Z",
            f"event_seen:{wf.id}": {"opencrq:t9:early": 1.0, "opencrq:t9:late": 1.0},
        }
    )

    async def _list(
        credential: str, event_type: str, since: str, *, tenant_id: str = "", limit: int
    ) -> tuple[list[dict[str, Any]], str, str, str | None]:
        return (
            [_run("early", "2026-11-06T07:02:00Z"), _run("late", "2026-11-06T07:05:00Z")],
            "https://crq",
            tenant_id,
            None,
        )

    monkeypatch.setattr(opencrq_job_events, "list_finished_runs", _list)
    events = await event_poller._poll_opencrq_job_runs(
        "https://crq||tok",
        "risk_scenario_recomputed",
        wf,
        redis,  # type: ignore[arg-type]
    )
    assert events == []


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
