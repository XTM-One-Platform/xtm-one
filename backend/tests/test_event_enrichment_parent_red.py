"""``enriched.head.parent``: a red the head commit did not cause (#752).

A bot commit on a pull request whose required check had failed for days woke
the red-check listener at every push, though nothing new had failed. The parent
facet places the head's counted reds against its first parent's check runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.github import _common as gh_common
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services.integration_event_filters import payload_filter_reason

REPO = "XTM-Flow/filigran-flow"
SHA = "c" * 40
PARENT = "p" * 40
GATE = "Flow RACI"
FILTER = {"action": "completed", "enriched.head.parent.unchanged_red": {"ne": True}}


def _run(run_id: int, name: str, conclusion: str | None = "failure", status: str = "completed") -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completed_at": "2026-11-19T15:00:00Z" if status == "completed" else None,
        "app": {"slug": "github-actions"},
    }


def _event() -> dict[str, Any]:
    return {
        "action": "completed",
        "check_run": {"id": 9, "head_sha": SHA, "completed_at": "2026-11-19T15:00:00Z"},
        "repository": {"full_name": REPO},
    }


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


def _github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_runs: list[dict[str, Any]],
    parent_runs: list[dict[str, Any]] | None = None,
    parents: list[dict[str, Any]] | None = None,
    commit_error: bool = False,
) -> list[str]:
    calls: list[str] = []

    async def _request(
        _client: Any, _method: str, url: str, *_a: Any, params: Any = None, **_k: Any
    ) -> tuple[_Resp | None, str | None]:
        calls.append(url)
        if url.endswith("/pulls"):
            return _Resp([]), None
        if url.endswith(f"/commits/{SHA}/check-runs"):
            return _Resp({"total_count": len(head_runs), "check_runs": head_runs}), None
        if url.endswith(f"/commits/{PARENT}/check-runs"):
            runs = parent_runs or []
            return _Resp({"total_count": len(runs), "check_runs": runs}), None
        if url.endswith(f"/commits/{SHA}"):
            if commit_error:
                return None, "Error: 502 Bad Gateway"
            return _Resp({"sha": SHA, "parents": [{"sha": PARENT}] if parents is None else parents}), None
        if url.endswith("/status"):
            return _Resp({"state": "success", "statuses": []}), None
        if url.endswith("/actions/runs"):
            return _Resp({"total_count": 0, "workflow_runs": []}), None
        raise AssertionError(url)

    monkeypatch.setattr(gh_common, "safe_request", _request)
    return calls


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("enriched.head.parent.unchanged_red",), frozenset({"parent"})),
        (("enriched.head.parent",), frozenset({"parent"})),
        (("enriched.head.checks.first_failure",), frozenset({"checks"})),
    ],
)
def test_the_parent_facet_follows_the_filter_keys(keys: tuple[str, ...], expected: frozenset[str]) -> None:
    assert gh_enrichment.facets("check_run.completed", _event(), keys) == expected


def test_a_red_already_red_on_the_parent_is_unchanged_and_a_new_one_is_not() -> None:
    parent = [_run(1, GATE), _run(2, "lint", "success")]
    facts = gh_enrichment.parent_facts([GATE], PARENT, parent)
    assert facts == {"sha": PARENT, "failed_names": [GATE], "new_failed_names": [], "unchanged_red": True}
    facts = gh_enrichment.parent_facts([GATE, "lint"], PARENT, parent)
    assert facts["unchanged_red"] is False and facts["new_failed_names"] == ["lint"]


def test_a_red_fixed_by_a_rerun_on_the_parent_counts_as_new() -> None:
    parent = [_run(1, GATE), _run(5, GATE, "success")]
    assert gh_enrichment.parent_facts([GATE], PARENT, parent)["unchanged_red"] is False


def test_no_red_or_no_parent_never_reads_unchanged() -> None:
    assert gh_enrichment.parent_facts([], PARENT, [_run(1, GATE)])["unchanged_red"] is False
    facts = gh_enrichment.parent_facts([GATE], None, None)
    assert facts["unchanged_red"] is False and facts["sha"] is None


@pytest.mark.asyncio
async def test_an_inherited_red_is_dropped_by_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _github(monkeypatch, head_runs=[_run(9, GATE)], parent_runs=[_run(1, GATE)])
    event = _event()
    facts = await gh_enrichment.fetch("check_run.completed", event, "tok", facets=frozenset({"parent"}))
    assert facts["head"]["parent"]["unchanged_red"] is True
    assert payload_filter_reason({**event, "enriched": facts}, FILTER) is not None
    assert any(url.endswith(f"/commits/{PARENT}/check-runs") for url in calls)


@pytest.mark.asyncio
async def test_a_new_red_is_kept_by_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, head_runs=[_run(9, GATE)], parent_runs=[_run(1, GATE, "success")])
    event = _event()
    facts = await gh_enrichment.fetch("check_run.completed", event, "tok", facets=frozenset({"parent"}))
    assert facts["head"]["parent"]["new_failed_names"] == [GATE]
    assert payload_filter_reason({**event, "enriched": facts}, FILTER) is None


@pytest.mark.asyncio
async def test_a_green_head_reads_no_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _github(monkeypatch, head_runs=[_run(9, GATE, "success")])
    facts = await gh_enrichment.fetch("check_run.completed", _event(), "tok", facets=frozenset({"parent"}))
    assert facts["head"]["parent"]["unchanged_red"] is False
    assert not any(url.endswith(f"/commits/{SHA}") for url in calls)


@pytest.mark.asyncio
async def test_an_unreadable_parent_keeps_the_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, head_runs=[_run(9, GATE)], commit_error=True)
    event = _event()
    facts = await gh_enrichment.fetch("check_run.completed", event, "tok", facets=frozenset({"parent"}))
    assert facts["head"]["parent"]["unchanged_red"] is False
    assert payload_filter_reason({**event, "enriched": facts}, FILTER) is None
