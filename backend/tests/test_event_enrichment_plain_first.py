"""A filter's plain keys decide before the enriched facts are read (#9450).

An ``integration_event`` filter mixing keys of the delivery (``action``, a
conclusion, a repository) with an ``enriched.`` key used to read the facts for
every delivery first: a requested run or a success the filter never wants paid
a GitHub read, and was counted "the enriched facts could not be read" when
that read timed out.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import webhooks
from app.integrations.event_enrichment import event_enricher
from app.services import event_enrichment
from app.services.integration_event_filters import plain_filter_reason

ME = "SamuelHassine"
_RED = {
    "action": "completed",
    "workflow_run.conclusion": {"in": ["failure", "cancelled"]},
    "workflow_run.head_branch": {"not_in": ["main", "master"]},
    "enriched.pull_request.user.login": ME,
    "repository.full_name": ["XTM-One-Platform/xtm-one"],
}
_CONFIG = {"provider": "github", "event": "workflow_run", "filter": _RED}
_AGENT = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None, enabled=True)


def _run(action: str = "completed", conclusion: str | None = "failure") -> dict:
    return {
        "action": action,
        "workflow_run": {
            "id": 1,
            "run_attempt": 1,
            "name": "CI",
            "conclusion": conclusion,
            "head_sha": "c" * 40,
            "head_branch": "fix/1",
            "pull_requests": [],
        },
        "repository": {"full_name": "XTM-One-Platform/xtm-one"},
    }


def _patch(monkeypatch, fetch) -> list[str]:
    calls: list[str] = []

    async def _token(_db, _provider, _integration_id, _agent):
        return "tok", "integration"

    async def _miss(_key):
        return None

    async def _remember(_key, _facts):
        return None

    async def _fetch(event_key, payload, token, **kwargs):
        calls.append(event_key)
        return await fetch(event_key, payload, token)

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(
        event_enrichment,
        "event_enricher",
        lambda _p: SimpleNamespace(
            applies=enricher.applies, cache_key=enricher.cache_key, fetch=_fetch, retryable=enricher.retryable
        ),
    )
    return calls


async def _slow(_event_key, _payload, _token):
    raise TimeoutError()


async def _mine(_event_key, _payload, _token):
    return {"pull_request": {"user": {"login": ME}}}


def test_plain_reason_names_the_plain_key():
    assert plain_filter_reason("workflow_run", _run(action="requested", conclusion=None), _RED) == (
        "action does not match the filter"
    )
    assert plain_filter_reason("workflow_run", _run(conclusion="success"), _RED) is not None
    assert plain_filter_reason("workflow_run", _run(), _RED) is None


def test_plain_reason_leaves_filters_without_enriched_keys_and_mixed_combinators():
    # No enriched key: the caller evaluates the filter whole, as before.
    assert plain_filter_reason("workflow_run", _run(action="requested"), {"action": "completed"}) is None
    # A combinator reading an enriched key is not plain: it waits for the facts.
    mixed = {"any": [{"action": "requested"}, {"enriched.pull_request.user.login": ME}]}
    assert plain_filter_reason("workflow_run", _run(), mixed) is None
    # A combinator reading only the delivery is plain and decides alone.
    plain_any = {"any": [{"action": "requested"}], "enriched.pull_request.user.login": ME}
    assert plain_filter_reason("workflow_run", _run(), plain_any) is not None


@pytest.mark.asyncio
async def test_a_delivery_the_plain_keys_drop_reads_no_fact(monkeypatch):
    calls = _patch(monkeypatch, _slow)
    for payload in (_run(action="requested", conclusion=None), _run(conclusion="success")):
        reason = await webhooks._assignment_filter_reason(
            None,
            "github",
            f"workflow_run.{payload['action']}",
            payload,
            event="workflow_run",
            trigger_config=_CONFIG,
            agent=_AGENT,
            assignment_id=str(uuid.uuid4()),
        )
        assert reason is not None and "could not be read" not in reason
    assert calls == []


@pytest.mark.asyncio
async def test_a_delivery_the_plain_keys_admit_still_reads_the_facts(monkeypatch):
    calls = _patch(monkeypatch, _mine)
    reason = await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _run(),
        event="workflow_run",
        trigger_config=_CONFIG,
        agent=_AGENT,
    )
    assert reason is None
    assert calls == ["workflow_run.completed"]


@pytest.mark.asyncio
async def test_an_outage_still_drops_a_delivery_only_the_facts_decide(monkeypatch):
    _patch(monkeypatch, _slow)
    reason = await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _run(),
        event="workflow_run",
        trigger_config=_CONFIG,
        agent=_AGENT,
    )
    assert reason is not None and "could not be read" in reason
