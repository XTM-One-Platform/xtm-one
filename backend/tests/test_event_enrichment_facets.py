"""The head's check runs are read only for a filter that reads them (#9420).

A filter on ``enriched.pull_request.*`` alone used to pay for the check-run
read of every CI event and was dropped when it timed out, GitHub answered 500,
or the head passed the 1000 check runs cap. The enricher now reads the check
runs only when a filter key reads ``enriched.head.checks`` (or the objects
holding it), and the facet is part of the cache key.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.integrations.event_enrichment import PermanentEnrichmentError, event_enricher
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services import event_enrichment
from app.services.integration_event_filters import payload_filter_reason

ME = "SamuelHassine"
REPO = "XTM-One-Platform/xtm-one"
SHA = "c" * 40


def _workflow_run() -> dict:
    return {
        "action": "completed",
        "workflow_run": {"id": 1, "run_attempt": 1, "head_sha": SHA, "conclusion": "failure", "pull_requests": []},
        "repository": {"full_name": REPO},
    }


def _pr(number: int) -> dict:
    return {
        "number": number,
        "state": "open",
        "draft": False,
        "user": {"login": ME},
        "head": {"sha": SHA, "ref": "fix/x", "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "labels": [],
    }


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _github(monkeypatch, *, check_runs_total: int = 1) -> list[str]:
    """GitHub answering one open pull request and *check_runs_total* check runs; the URLs read."""
    calls: list[str] = []

    async def _request(_client, _method, url, *_a, params=None, **_k):
        calls.append(url)
        if url.endswith("/pulls"):
            return _Resp([_pr(7)]), None
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": 0, "workflow_runs": []}), None
        if url.endswith("/status"):
            return _Resp({"state": "success", "statuses": []}), None
        # Distinct checks: the latest run of each check stands (#11807).
        page = [
            {"id": i + 1, "name": f"x{i}", "status": "completed", "conclusion": "success"}
            for i in range(gh_enrichment.CHECKS_PER_PAGE)
        ]
        return _Resp({"total_count": check_runs_total, "check_runs": page}), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    return calls


def _checks_read(calls: list[str]) -> bool:
    return any(url.endswith("/check-runs") for url in calls)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("enriched.pull_request.user.login",), frozenset()),
        (("action", "enriched.pull_request", "enriched.pull_requests"), frozenset()),
        (("enriched.head.sha",), frozenset()),
        (("enriched.head.checks.all_green",), frozenset({"checks"})),
        (("enriched.head.checks",), frozenset({"checks"})),
        (("enriched.head",), frozenset({"checks"})),
        (("enriched",), frozenset({"checks"})),
    ],
)
def test_the_github_facets_follow_the_filter_keys(keys, expected):
    assert gh_enrichment.facets("workflow_run.completed", _workflow_run(), keys) == expected


def test_a_push_never_reads_check_runs():
    push = {"after": SHA, "repository": {"full_name": REPO}}
    assert gh_enrichment.facets("push", push, ("enriched.head.checks.all_green",)) == frozenset()


def test_the_github_enricher_declares_its_facets():
    assert event_enricher("github").facets is gh_enrichment.facets


@pytest.mark.asyncio
async def test_a_pull_request_only_read_calls_no_check_runs(monkeypatch):
    calls = _github(monkeypatch)
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(), "tok", facets=frozenset())
    assert facts["pull_request"]["number"] == 7
    assert facts["head"] == {"sha": SHA}
    assert not _checks_read(calls)


@pytest.mark.asyncio
async def test_a_checks_read_and_a_legacy_call_still_read_the_check_runs(monkeypatch):
    calls = _github(monkeypatch)
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(), "tok", facets=frozenset({"checks"}))
    assert facts["head"]["checks"]["total"] == gh_enrichment.CHECKS_PER_PAGE
    assert _checks_read(calls)
    calls.clear()
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(), "tok")
    assert "checks" in facts["head"] and _checks_read(calls)


@pytest.mark.asyncio
async def test_a_head_past_the_cap_no_longer_drops_a_pull_request_filter(monkeypatch):
    _github(monkeypatch, check_runs_total=5000)
    with pytest.raises(PermanentEnrichmentError):
        await gh_enrichment.fetch("workflow_run.completed", _workflow_run(), "tok", facets=frozenset({"checks"}))
    facts = await gh_enrichment.fetch("workflow_run.completed", _workflow_run(), "tok", facets=frozenset())
    flt = {"action": "completed", "enriched.pull_request.user.login": ME}
    assert payload_filter_reason({**_workflow_run(), "enriched": facts}, flt) is None


def _patch_service(monkeypatch, cache: dict[str, dict]) -> list[frozenset[str] | None]:
    """The enrichment service on the real GitHub enricher with a recorded fetch; the facets each read asked."""
    asked: list[frozenset[str] | None] = []

    async def _token(_db, _provider, _integration_id, _agent):
        return "tok", "integration"

    async def _cached(key):
        return cache.get(key)

    async def _remember(key, facts):
        cache[key] = facts

    async def _fetch(_event_key, _payload, _token, *, facets=None):
        asked.append(facets)
        facts = {"pull_request": {"user": {"login": ME}}, "pull_requests": [], "head": {"sha": SHA}}
        if facets is None or "checks" in facets:
            facts["head"]["checks"] = {"all_green": True}
        return facts

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _cached)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(
        event_enrichment,
        "event_enricher",
        lambda _p: SimpleNamespace(
            applies=enricher.applies,
            cache_key=enricher.cache_key,
            fetch=_fetch,
            retryable=enricher.retryable,
            facets=enricher.facets,
        ),
    )
    return asked


_AGENT = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None, enabled=True)


def _config(flt: dict) -> dict:
    return {"provider": "github", "event": "workflow_run", "filter": flt}


@pytest.mark.asyncio
async def test_the_service_passes_the_facets_and_keys_the_cache_on_them(monkeypatch):
    cache: dict[str, dict] = {}
    asked = _patch_service(monkeypatch, cache)
    pr_only = _config({"enriched.pull_request.user.login": ME})
    green = _config({"enriched.head.checks.all_green": True})

    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(), trigger_config=pr_only, agent=_AGENT
    )
    assert asked == [frozenset()] and "checks" not in facts["head"]
    # The pull-requests-only answer in the cache never answers a checks filter.
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _workflow_run(), trigger_config=green, agent=_AGENT
    )
    assert asked == [frozenset(), frozenset({"checks"})] and facts["head"]["checks"]["all_green"] is True
    assert len(cache) == 2
    # Each facet is answered from its own cache entry afterwards.
    for config in (pr_only, green):
        await event_enrichment.enrichment_for(
            None, "github", "workflow_run.completed", _workflow_run(), trigger_config=config, agent=_AGENT
        )
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_memo_shared_by_two_assignments_keeps_the_facets_apart(monkeypatch):
    asked = _patch_service(monkeypatch, {})
    memo: dict[str, dict] = {}
    for flt in ({"enriched.pull_request.user.login": ME}, {"enriched.head.checks.all_green": True}):
        await event_enrichment.enrichment_for(
            None,
            "github",
            "workflow_run.completed",
            _workflow_run(),
            trigger_config=_config(flt),
            agent=_AGENT,
            memo=memo,
        )
    assert asked == [frozenset(), frozenset({"checks"})]
