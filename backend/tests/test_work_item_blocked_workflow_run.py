"""A work item blocked on the first successful run of a workflow after a time.

``blocked_on_refs`` kind ``workflow_run`` waits for a release run that deploys a
change merged in another repository. Unit level: the normaliser, the labels,
the GitHub runs answer, the reader's registration, and the release applied by
the polling fallback (released, still waiting, nothing found, one read per
reference and pass).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations import reference_state
from app.integrations.providers.github.reference_state import first_successful_run
from app.services import work_item_blocked_refs as refs_mod
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    blocked_why,
    normalize_blocked_on_refs,
    read_kind,
    workflow_run_note,
)

REPO = "xtm-one-platform/release"
AFTER = "2026-11-06T00:40:00Z"
NOW = datetime(2026, 11, 6, 3, 30, tzinfo=UTC)


def _wf(**extra) -> dict:
    return {
        "repo": REPO,
        "kind": "workflow_run",
        "workflow": "release.yml",
        "after": AFTER,
        "release_on": "succeeded",
        **extra,
    }


# -- the normaliser ---------------------------------------------------------------


def test_a_workflow_run_reference_is_normalised():
    refs = normalize_blocked_on_refs(
        [
            {
                "repo": "XTM-One-Platform/release",
                "kind": "workflow_run",
                "workflow": " release.yml ",
                "after": "2026-11-06T01:40:00+01:00",
                "branch": "main",
            }
        ]
    )
    assert refs == [_wf(branch="main")]


def test_after_defaults_to_the_write_and_a_workflow_id_is_accepted():
    (ref,) = normalize_blocked_on_refs([{"repo": REPO, "kind": "workflow_run", "workflow": 4242}])
    assert ref["workflow"] == "4242" and ref["after"].endswith("Z") and "branch" not in ref


def test_the_recorded_run_survives_a_rewrite():
    ref = _wf(state="succeeded", at="x", run_id=42, run_url="https://x", run_at=AFTER)
    assert normalize_blocked_on_refs([ref]) == [ref]


def test_title_contains_is_kept_and_makes_its_own_reference() -> None:
    refs = normalize_blocked_on_refs([_wf(title_contains="refs/tags/"), _wf()])
    assert refs == [_wf(title_contains="refs/tags/"), _wf()]


def test_a_blank_title_contains_is_no_filter() -> None:
    (ref,) = normalize_blocked_on_refs([_wf(title_contains="")])
    assert "title_contains" not in ref


@pytest.mark.parametrize("title", ["   ", 7, "x" * 201])
def test_a_title_contains_the_platform_cannot_match_is_refused(title: object) -> None:
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([_wf(title_contains=title)])


def test_two_waits_on_the_same_workflow_after_different_times_are_two_references():
    refs = normalize_blocked_on_refs([_wf(), _wf(after="2026-11-06T02:00:00Z"), _wf()])
    assert len(refs) == 2


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": REPO, "kind": "workflow_run"},
        {"repo": REPO, "kind": "workflow_run", "workflow": "../x"},
        {"repo": REPO, "kind": "workflow_run", "workflow": "release.yml", "after": "yesterday"},
        {"repo": REPO, "kind": "workflow_run", "workflow": "release.yml", "release_on": "merged"},
        {"repo": REPO, "kind": "workflow_run", "workflow": "release.yml", "branch": "a b"},
        {"repo": REPO, "kind": "pull_request", "number": 1, "release_on": "succeeded"},
    ],
)
def test_a_workflow_run_the_platform_cannot_match_is_refused(entry):
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([entry])


# -- the labels -------------------------------------------------------------------


def _blocked(refs: list) -> SimpleNamespace:
    return SimpleNamespace(
        status="blocked", blocked_on=None, blocked_on_refs=refs, blocked_reason=None, needs_human=None, action_mode=None
    )


def test_the_waits_for_line_names_the_workflow_and_the_time():
    assert blocked_why(_blocked([_wf(branch="main")]))["text"] == (
        f"Waits for: a successful {REPO} release.yml run on main after {AFTER}"
    )
    assert blocked_why(_blocked([_wf(state="succeeded")]))["text"] == "No reason given"
    titled = blocked_why(_blocked([_wf(title_contains="refs/tags/")]))
    assert titled is not None and titled["text"] == (
        f"Waits for: a successful {REPO} release.yml run titled '*refs/tags/*' after {AFTER}"
    )


def test_a_workflow_run_is_never_read_as_a_pull_request():
    assert read_kind(_wf()) is None


def test_the_release_note_names_the_run():
    run = {
        "id": 7,
        "url": "https://github.com/r/actions/runs/7",
        "created_at": AFTER,
        "updated_at": "2026-11-06T03:10:00Z",
    }
    assert workflow_run_note(_wf(), run, NOW) == (
        f"released: a successful {REPO} release.yml run after {AFTER}: run 7 "
        "(https://github.com/r/actions/runs/7) succeeded at 2026-11-06T03:10:00Z; released at 2026-11-06T03:30:00Z"
    )


# -- the GitHub runs answer -------------------------------------------------------


def test_the_first_successful_run_after_the_time_is_picked():
    data = {
        "workflow_runs": [
            {"id": 3, "conclusion": "success", "created_at": "2026-11-06T03:00:00Z", "html_url": "u3"},
            {
                "id": 2,
                "conclusion": "success",
                "created_at": "2026-11-06T02:00:00Z",
                "html_url": "u2",
                "head_sha": "ab",
            },
            {"id": 1, "conclusion": "success", "created_at": "2026-11-06T00:10:00Z"},
            {"id": 4, "conclusion": "failure", "created_at": "2026-11-06T01:00:00Z"},
        ]
    }
    run = first_successful_run(data, AFTER)
    assert run == {
        "id": 2,
        "url": "u2",
        "created_at": "2026-11-06T02:00:00Z",
        "updated_at": None,
        "head_sha": "ab",
        "display_title": None,
    }


def test_title_contains_skips_the_main_run_of_a_release_workflow() -> None:
    # release.yml runs for a push on main and for the tag: only the tag build deploys (#34).
    data = {
        "workflow_runs": [
            {
                "id": 5,
                "conclusion": "success",
                "created_at": "2026-11-06T01:10:00Z",
                "display_title": "Release refs/tags/v1",
            },
            {
                "id": 4,
                "conclusion": "success",
                "created_at": "2026-11-06T01:00:00Z",
                "display_title": "Release refs/heads/main",
            },
        ]
    }
    first = first_successful_run(data, AFTER)
    assert first is not None and first["id"] == 4
    tagged = first_successful_run(data, AFTER, "refs/tags/")
    assert tagged is not None and tagged["id"] == 5 and tagged["display_title"] == "Release refs/tags/v1"
    assert first_successful_run(data, AFTER, "refs/tags/v2") is None


def test_no_successful_run_after_the_time_answers_none():
    old = {"workflow_runs": [{"id": 1, "conclusion": "success", "created_at": "2026-11-05T00:00:00Z"}]}
    assert first_successful_run(old, AFTER) is None
    assert first_successful_run({"message": "Not Found"}, AFTER) is None
    assert first_successful_run("nope", AFTER) is None


def test_github_registers_its_workflow_run_reader():
    import app.integrations.providers.github  # noqa: F401 - registers the reader

    assert "github" in reference_state.workflow_run_readers()


# -- the release by the polling fallback -----------------------------------------


class _Result:
    def __init__(self, rows: Any = None, item: Any = None, one: Any = None) -> None:
        self._rows, self._item, self._one = rows or [], item, one

    def one_or_none(self) -> Any:
        return self._one

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._item


class _Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        item = self.store["item"]
        return _Result(
            rows=[(item.id, item.user_id, list(item.blocked_on_refs or []))],
            item=item,
            one=(item.user_id, item.status, list(item.blocked_on_refs or [])),
        )

    async def commit(self):
        self.store["commits"] += 1


@pytest.fixture
def poll(monkeypatch):
    item = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="blocked",
        blocked_on=None,
        blocked_on_refs=[_wf()],
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
    run = {"id": 9, "url": "https://x/9", "created_at": "2026-11-06T02:00:00Z", "updated_at": "2026-11-06T02:20:00Z"}
    store: dict = {"item": item, "commits": 0, "reads": [], "run": run, "published": []}

    async def token(db, provider, user_id):
        return "tok"

    async def reader(
        repo: str, workflow: str, after: str, branch: str | None, title_contains: str | None, tok: str
    ) -> dict[str, Any] | None:
        store["reads"].append((repo, workflow, after, branch, title_contains))
        return store["run"]

    async def published(item_id, note, run, matched):
        store["published"].append((item_id, note))

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(store))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_publish_workflow_run", published)
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(refs_mod, "_budget_skipped", set())
    monkeypatch.setattr(reference_state, "_READERS", {})
    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": reader})
    return store


async def test_a_successful_run_releases_the_item(poll):
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    item = poll["item"]
    assert counts["read"] == 1 and counts["released"] == 1
    assert poll["reads"] == [(REPO, "release.yml", AFTER, None, None)]
    assert item.status == "open" and item.blocked_on_refs is None and item.claimed_by_agent_id is None
    assert item.description.startswith(f"released: a successful {REPO} release.yml run after {AFTER}: run 9")
    assert len(poll["published"]) == 1


async def test_the_title_filter_reaches_the_reader(poll: dict[str, Any]) -> None:
    poll["item"].blocked_on_refs = [_wf(title_contains="refs/tags/")]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["released"] == 1
    assert poll["reads"] == [(REPO, "release.yml", AFTER, None, "refs/tags/")]
    assert "run titled '*refs/tags/*' after" in poll["item"].description


async def test_no_run_yet_keeps_it_blocked(poll):
    poll["run"] = None
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts == {"read": 1, "released": 0, "noted": 0}
    assert poll["item"].status == "blocked" and poll["commits"] == 0


# -- the release by the workflow_run delivery --------------------------------------


def _delivery(**extra) -> Any:
    from app.integrations.pull_request_events import PullRequestEvent

    fields: dict[str, Any] = {
        "kind": "ci",
        "repo": REPO,
        "head_branch": "main",
        "run_key": "workflow:4242",
        "run_id": 9,
        "status": "completed",
        "conclusion": "success",
        "extra": {"base_run": True, "workflow_file": "release.yml"},
    }
    fields.update(extra)
    return PullRequestEvent(**fields)


def test_a_successful_run_names_its_file_and_its_id() -> None:
    assert refs_mod.delivered_workflow_names(_delivery()) == frozenset({"4242", "release.yml"})


@pytest.mark.parametrize(
    "extra",
    [{"conclusion": "failure"}, {"status": "in_progress", "conclusion": None}, {"run_key": "check_suite:1"}],
)
def test_a_run_not_successful_or_not_a_workflow_names_nothing(extra: dict) -> None:
    assert refs_mod.delivered_workflow_names(_delivery(**extra)) == frozenset()
    assert refs_mod.named_by_workflow_run([_wf()], _delivery(**extra)) == []


def test_the_delivery_names_the_reference_of_its_repo_workflow_and_branch() -> None:
    event = _delivery()
    assert refs_mod.named_by_workflow_run([_wf()], event) == [_wf()]
    assert refs_mod.named_by_workflow_run([_wf(workflow="4242")], event) == [_wf(workflow="4242")]
    assert refs_mod.named_by_workflow_run([_wf(branch="main")], event) == [_wf(branch="main")]
    assert refs_mod.named_by_workflow_run([_wf(branch="release/1")], event) == []
    assert refs_mod.named_by_workflow_run([_wf(workflow="ci.yml")], event) == []
    assert refs_mod.named_by_workflow_run([_wf(repo="o/other")], event) == []
    assert refs_mod.named_by_workflow_run([_wf(state="succeeded")], event) == []


async def test_a_successful_run_delivery_releases_the_item_after_its_read_back(poll):
    counts = await refs_mod.release_from_workflow_run_delivery(_delivery())
    item = poll["item"]
    assert counts == {"read": 1, "released": 1, "noted": 0}
    assert poll["reads"] == [(REPO, "release.yml", AFTER, None, None)]
    assert item.status == "open" and item.blocked_on_refs is None
    assert len(poll["published"]) == 1


async def test_the_read_back_decides_not_the_delivery(poll):
    # The delivery says a run succeeded, the code host knows none after the time: nothing moves.
    poll["run"] = None
    counts = await refs_mod.release_from_workflow_run_delivery(_delivery())
    assert counts == {"read": 1, "released": 0, "noted": 0}
    assert poll["item"].status == "blocked" and poll["commits"] == 0


async def test_a_failed_run_delivery_reads_nothing(poll):
    counts = await refs_mod.release_from_workflow_run_delivery(_delivery(conclusion="failure"))
    assert counts["read"] == 0 and poll["reads"] == [] and poll["item"].status == "blocked"


async def test_a_read_failure_of_the_delivery_is_counted_and_never_raises(poll, monkeypatch):
    async def broken(*args: Any) -> None:
        raise RuntimeError("401 bad credentials")

    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": broken})
    counts = await refs_mod.release_from_workflow_run_delivery(_delivery())
    assert counts == {"read": 1, "released": 0, "noted": 0, "read_failed": 1}
    assert poll["item"].status == "blocked"


# -- the check once at the write that blocks it (a run that succeeded before) -----


async def test_a_run_already_succeeded_releases_the_item_at_the_write(poll: dict[str, Any]) -> None:
    counts = await refs_mod.check_item_workflow_runs(poll["item"].id)
    item = poll["item"]
    assert counts == {"read": 1, "released": 1, "noted": 0}
    assert poll["reads"] == [(REPO, "release.yml", AFTER, None, None)]
    assert item.status == "open" and item.blocked_on_refs is None
    assert len(poll["published"]) == 1


async def test_no_run_yet_at_the_write_keeps_it_blocked(poll: dict[str, Any]) -> None:
    poll["run"] = None
    counts = await refs_mod.check_item_workflow_runs(poll["item"].id)
    assert counts == {"read": 1, "released": 0, "noted": 0}
    assert poll["item"].status == "blocked" and poll["commits"] == 0


async def test_the_write_check_reads_nothing_for_an_item_not_blocked_or_without_a_run_ref(
    poll: dict[str, Any],
) -> None:
    poll["item"].status = "open"
    assert await refs_mod.check_item_workflow_runs(poll["item"].id) == {"read": 0, "released": 0, "noted": 0}
    poll["item"].status = "blocked"
    poll["item"].blocked_on_refs = [{"repo": "o/r", "kind": "issue", "number": 3, "release_on": "closed"}]
    assert await refs_mod.check_item_workflow_runs(poll["item"].id) == {"read": 0, "released": 0, "noted": 0}
    assert poll["reads"] == []


async def test_a_read_failure_at_the_write_never_raises(poll: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any) -> None:
        raise RuntimeError("401 bad credentials")

    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": broken})
    counts = await refs_mod.check_item_workflow_runs(poll["item"].id)
    assert counts["released"] == 0 and counts.get("read_failed") == 1
    assert poll["item"].status == "blocked"


def test_the_write_schedules_the_check_only_for_a_workflow_run_reference() -> None:
    from app.services.work_item import _names_workflow_run

    assert _names_workflow_run([_wf()])
    assert not _names_workflow_run([{"repo": "o/r", "kind": "deployment", "number": 1}])
    assert not _names_workflow_run(None)


async def test_one_satisfied_reference_of_two_leaves_it_waiting(poll):
    item = poll["item"]
    item.blocked_on_refs = [_wf(), {"repo": "o/r", "kind": "issue", "number": 3, "release_on": "closed"}]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["noted"] == 1 and item.status == "blocked"
    assert item.blocked_on_refs[0]["state"] == "succeeded" and item.blocked_on_refs[0]["run_id"] == 9
    assert item.claim_notes[-1]["text"].endswith("; still waiting")


# -- a workflow the code host does not know (#386) ---------------------------------


async def test_a_workflow_github_does_not_know_is_a_read_failure_not_no_run_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.providers.github import _common
    from app.integrations.providers.github import reference_state as gh

    async def not_found(*args: Any, **kwargs: Any) -> tuple[Any, str]:
        assert kwargs.get("expected_statuses") == {404}
        return SimpleNamespace(status_code=404), "GitHub: Not Found (404)"

    monkeypatch.setattr(_common, "safe_request", not_found)
    with pytest.raises(LookupError, match=r"workflow relase\.yml not found in o/r"):
        await gh.read_successful_workflow_run("o/r", "relase.yml", AFTER, None, None, "tok")


async def test_another_error_still_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.github import _common
    from app.integrations.providers.github import reference_state as gh

    async def server_error(*args: Any, **kwargs: Any) -> tuple[Any, str]:
        return SimpleNamespace(status_code=502), "GitHub: Bad Gateway (502)"

    monkeypatch.setattr(_common, "safe_request", server_error)
    assert await gh.read_successful_workflow_run("o/r", "release.yml", AFTER, None, None, "tok") is None


async def test_an_unknown_workflow_is_told_on_the_item_at_the_write(
    poll: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(*args: Any) -> None:
        raise LookupError(f"workflow relase.yml not found in {REPO} (404)")

    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": missing})
    item = poll["item"]
    item.last_activity_at = datetime.now(UTC)  # just blocked: the poller's quiet time would hold the note
    counts = await refs_mod.check_item_workflow_runs(item.id)
    assert counts["released"] == 0 and counts.get("read_failed") == 1
    assert item.status == "blocked"
    assert item.claim_notes[-1]["text"].startswith(refs_mod.READ_FAILURE_NOTE_PREFIX)
    assert "workflow relase.yml not found" in item.claim_notes[-1]["text"]
    # One note per 6 hours still holds: a second write says nothing new.
    await refs_mod.check_item_workflow_runs(item.id)
    assert len(item.claim_notes) == 1
