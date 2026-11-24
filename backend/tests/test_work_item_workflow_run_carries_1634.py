"""A ``workflow_run`` reference that waits for the release of a merge (#1634).

``carries_sha`` (with ``carries_repo``) releases the item only on a successful run
whose display title names that commit or a descendant of it: a release of an older
commit moves the read past it, a release of the exact commit cancelled by a newer
push never strands the item.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations import reference_state
from app.services import work_item_blocked_refs as refs_mod
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    normalize_blocked_on_refs,
    ref_label,
    run_commit,
    unobserved_deployment_message,
)

REPO = "opencrq-platform/release"
SOURCE = "opencrq-platform/opencrq"
AFTER = "2026-11-24T00:00:00Z"
NOW = datetime(2026, 11, 24, 3, 0, tzinfo=UTC)
MERGE = "e860cccfb902ae962a5d3abe8319a9de7b9a82d2"
OLDER = "81ff1453a698d937360cbbc69c820c31ce8adbbf"
LATER = "1cbb58f2da587aaba00bc624db822b3e3662b60d"


def _wf(**extra: Any) -> dict[str, Any]:
    return {
        "repo": REPO,
        "kind": "workflow_run",
        "workflow": "release.yml",
        "after": AFTER,
        "release_on": "succeeded",
        **extra,
    }


def _run(run_id: int, created: str, sha: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "url": f"https://x/{run_id}",
        "created_at": created,
        "updated_at": created,
        "display_title": f"Release refs/heads/main at {sha}",
    }


# -- the normaliser -----------------------------------------------------------------


def test_carries_is_split_into_sha_and_repo_lower_case() -> None:
    (ref,) = normalize_blocked_on_refs([_wf(carries=f"OpenCRQ-Platform/opencrq@{MERGE.upper()}")])
    assert ref["carries_sha"] == MERGE and ref["carries_repo"] == SOURCE and "carries" not in ref
    # A rewrite of the stored reference keeps it.
    assert normalize_blocked_on_refs([ref]) == [ref]


def test_a_bare_sha_or_the_reference_s_own_repo_names_no_carries_repo() -> None:
    (bare,) = normalize_blocked_on_refs([_wf(carries=MERGE[:7])])
    (own,) = normalize_blocked_on_refs([_wf(carries=f"{REPO}@{MERGE}")])
    assert bare["carries_sha"] == MERGE[:7] and "carries_repo" not in bare
    assert own["carries_sha"] == MERGE and "carries_repo" not in own


@pytest.mark.parametrize("bad", ["xyz", "12", 42, "opencrq@" + MERGE, "a/b c@" + MERGE])
def test_a_carries_that_names_no_commit_is_refused(bad: object) -> None:
    with pytest.raises(BlockedRefError, match="carries must be a commit"):
        normalize_blocked_on_refs([_wf(carries=bad)])


def test_two_merges_are_two_references() -> None:
    assert len(normalize_blocked_on_refs([_wf(carries=MERGE), _wf(carries=LATER)])) == 2


def test_the_label_and_the_refusal_name_the_carried_commit() -> None:
    label = ref_label(normalize_blocked_on_refs([_wf(carries=f"{SOURCE}@{MERGE}")])[0])
    assert f"carrying {SOURCE}@{MERGE[:7]}" in label
    assert f'"carries": "{SOURCE}@" followed by the merge commit' in unobserved_deployment_message(SOURCE)


def test_run_commit_reads_the_title() -> None:
    assert run_commit(_run(1, AFTER, MERGE)) == MERGE
    assert run_commit({"display_title": "Release refs/heads/main"}) is None
    assert run_commit({}) is None


# -- the release by the polling fallback -----------------------------------------------


class _Result:
    def __init__(self, item: Any) -> None:
        self._item = item

    def all(self) -> list[tuple[Any, Any, list[Any]]]:
        return [(self._item.id, self._item.user_id, list(self._item.blocked_on_refs or []))]

    def scalar_one_or_none(self) -> Any:
        return self._item

    def one_or_none(self) -> Any:
        return (self._item.user_id, self._item.status, list(self._item.blocked_on_refs or []))


class _Session:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: Any) -> _Result:
        return _Result(self.store["item"])

    async def commit(self) -> None:
        self.store["commits"] += 1


@pytest.fixture
def poll(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="blocked",
        blocked_on=None,
        blocked_on_refs=normalize_blocked_on_refs([_wf(carries=f"{SOURCE}@{MERGE}")]),
        blocked_reason=None,
        needs_human=None,
        claim_notes=[],
        description="brief",
        not_before=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=None,
        claimed_at=None,
        last_activity_at=None,
    )
    # Successful runs by creation time; the reader answers the oldest at or after ``after``.
    store: dict[str, Any] = {"item": item, "commits": 0, "reads": [], "runs": [], "contains": {}, "published": []}

    async def token(db: Any, provider: str, user_id: Any) -> str:
        return "tok"

    async def reader(
        repo: str, workflow: str, after: str, branch: str | None, title: str | None, tok: str
    ) -> dict[str, Any] | None:
        store["reads"].append(after)
        later = [r for r in store["runs"] if r["created_at"] >= after]
        return min(later, key=lambda r: r["created_at"]) if later else None

    async def contains(repo: str, commit: str, build: str, tok: str) -> bool | None:
        store.setdefault("containment_reads", []).append((repo, commit, build))
        answer: bool | None = store["contains"].get(build)
        return answer

    async def published(item_id: Any, note: str, run: dict[str, Any], matched: list[Any]) -> None:
        store["published"].append(note)

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(store))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_publish_workflow_run", published)
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(refs_mod, "_budget_skipped", set())
    monkeypatch.setattr(reference_state, "_READERS", {})
    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": reader})
    monkeypatch.setattr(reference_state, "_CONTAINMENT_READERS", {"github": contains})
    return store


async def test_the_release_of_the_merge_itself_releases_the_item(poll: dict[str, Any]) -> None:
    poll["runs"] = [_run(80, "2026-11-24T00:52:00Z", MERGE)]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 1 and poll["item"].status == "open"
    assert "containment_reads" not in poll


async def test_an_older_commit_s_release_is_skipped_for_the_next_one(poll: dict[str, Any]) -> None:
    poll["runs"] = [_run(79, "2026-11-24T00:39:41Z", OLDER), _run(81, "2026-11-24T01:10:13Z", LATER)]
    poll["contains"] = {OLDER: False, LATER: True}
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 1
    assert poll["reads"] == [AFTER, "2026-11-24T00:39:42Z"]
    assert poll["containment_reads"] == [(SOURCE, MERGE, OLDER), (SOURCE, MERGE, LATER)]
    (note,) = poll["published"]
    assert "run 81" in note


async def test_only_older_releases_keep_it_blocked(poll: dict[str, Any]) -> None:
    poll["runs"] = [_run(79, "2026-11-24T00:39:41Z", OLDER)]
    poll["contains"] = {OLDER: False}
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 0 and poll["item"].status == "blocked" and poll["commits"] == 0


async def test_an_unreadable_containment_releases_nothing(poll: dict[str, Any]) -> None:
    poll["runs"] = [_run(81, "2026-11-24T01:10:13Z", LATER)]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 0 and poll["item"].status == "blocked"


async def test_a_title_without_a_commit_is_skipped(poll: dict[str, Any]) -> None:
    poll["runs"] = [
        {"id": 7, "created_at": "2026-11-24T00:10:00Z", "display_title": "Release"},
        _run(80, "2026-11-24T00:52:00Z", MERGE),
    ]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 1 and "run 80" in poll["published"][0]
