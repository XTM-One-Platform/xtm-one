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
    assert run == {"id": 2, "url": "u2", "created_at": "2026-11-06T02:00:00Z", "updated_at": None, "head_sha": "ab"}


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
    def __init__(self, rows=None, item=None):
        self._rows, self._item = rows or [], item

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
        return _Result(rows=[(item.id, item.user_id, list(item.blocked_on_refs or []))], item=item)

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

    async def reader(repo, workflow, after, branch, tok):
        store["reads"].append((repo, workflow, after, branch))
        return store["run"]

    async def published(item_id, note, run, matched):
        store["published"].append((item_id, note))

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(store))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_publish_workflow_run", published)
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(reference_state, "_READERS", {})
    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {"github": reader})
    return store


async def test_a_successful_run_releases_the_item(poll):
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    item = poll["item"]
    assert counts["read"] == 1 and counts["released"] == 1
    assert poll["reads"] == [(REPO, "release.yml", AFTER, None)]
    assert item.status == "open" and item.blocked_on_refs is None and item.claimed_by_agent_id is None
    assert item.description.startswith(f"released: a successful {REPO} release.yml run after {AFTER}: run 9")
    assert len(poll["published"]) == 1


async def test_no_run_yet_keeps_it_blocked(poll):
    poll["run"] = None
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts == {"read": 1, "released": 0, "noted": 0}
    assert poll["item"].status == "blocked" and poll["commits"] == 0


async def test_one_satisfied_reference_of_two_leaves_it_waiting(poll):
    item = poll["item"]
    item.blocked_on_refs = [_wf(), {"repo": "o/r", "kind": "issue", "number": 3, "release_on": "closed"}]
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts["noted"] == 1 and item.status == "blocked"
    assert item.blocked_on_refs[0]["state"] == "succeeded" and item.blocked_on_refs[0]["run_id"] == 9
    assert item.claim_notes[-1]["text"].endswith("; still waiting")
