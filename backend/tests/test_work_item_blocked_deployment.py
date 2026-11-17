"""A work item blocked on the deployment of a change.

``blocked_on_refs`` kind ``deployment`` waits for a pull request's merge,
then for a build whose ``git_sha`` carries the merge commit. Unit level: the
normaliser, the merge stage in the pure matcher, the Waits for line before
and after, the GitHub compare answer, the polling fallback reading only the
merge stage, and the deploy stage run on ``platform.release.deployed`` (one
containment read per merge and build, the release and its note, a build
that does not carry it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.integrations import reference_state
from app.integrations.providers.github.reference_state import contains_commit
from app.integrations.pull_request_events import PullRequestEvent
from app.services import platform_builds
from app.services import work_item_blocked_refs as refs_mod
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    apply_reference_event,
    blocked_why,
    deployed_note,
    normalize_blocked_on_refs,
    pending_deployments,
    read_kind,
)

REPO = "xtm-one-platform/xtm-one"
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
MERGE = "f168aa7671e341c2f2a943ae2bad1fec268354a9"
BUILD = "6be48754aaaabbbbccccddddeeeeffff00001111"


def _dep(number: int = 8412, **extra) -> dict:
    return {"repo": REPO, "kind": "deployment", "number": number, "release_on": "deployed", **extra}


# -- the normaliser ---------------------------------------------------------------


def test_a_deployment_reference_is_normalised_with_its_default():
    refs = normalize_blocked_on_refs(
        [
            {"repo": "XTM-One-Platform/xtm-one", "kind": "deployment", "number": "#8412"},
            {"repo": "XTM-One-Platform/xtm-one", "kind": "deployment", "sha": MERGE.upper()},
            {"repo": "XTM-One-Platform/xtm-one", "number": 8412},
        ]
    )
    assert refs[0] == _dep()
    assert refs[1] == {"repo": REPO, "kind": "deployment", "release_on": "deployed", "sha": MERGE, "merge_sha": MERGE}
    # A pull request reference and a deployment of the same number are two references.
    assert refs[2]["kind"] == "pull_request" and len(refs) == 3


def test_the_recorded_stages_survive_a_rewrite():
    ref = _dep(merge_sha=MERGE, state="merged", at="x", build="1.0.0")
    assert normalize_blocked_on_refs([ref]) == [ref]


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": REPO, "kind": "deployment"},
        {"repo": REPO, "kind": "deployment", "sha": "not-a-sha"},
        {"repo": REPO, "kind": "deployment", "number": 1, "release_on": "merged"},
        {"repo": REPO, "kind": "pull_request", "number": 1, "release_on": "deployed"},
    ],
)
def test_a_deployment_the_platform_cannot_match_is_refused(entry):
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([entry])


# -- the merge stage ----------------------------------------------------------------


def test_the_merge_records_the_merge_commit_and_keeps_the_item_blocked():
    event = PullRequestEvent(
        kind="closed", repo=REPO, pr_numbers=(8412,), merged=True, merge_sha=MERGE, closed_at="2026-11-04T12:59:11Z"
    )
    out, released, refused = apply_reference_event([_dep()], event, NOW)
    assert out[0]["merge_sha"] == MERGE and out[0]["merged_at"] == "2026-11-04T12:59:11Z"
    assert released == [f"deployment of {REPO}#8412: merged f168aa7"] and refused == []
    # Merged is not deployed: the item still waits.
    assert not refs_mod.all_satisfied(out)
    assert pending_deployments(out) == out


def test_a_pull_request_closed_unmerged_keeps_the_deployment_waiting():
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(8412,), merged=False)
    out, released, refused = apply_reference_event([_dep()], event, NOW)
    assert released == [] and refused and "closed unmerged" in refused[0]
    assert "merge_sha" not in out[0]


def test_an_issue_or_another_number_never_matches_a_deployment():
    issue = PullRequestEvent(kind="issue_closed", repo=REPO, pr_numbers=(8412,))
    other = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(1,), merged=True, merge_sha=MERGE)
    for event in (issue, other):
        _, released, refused = apply_reference_event([_dep()], event, NOW)
        assert released == [] and refused == []


def test_only_the_merge_stage_is_read_from_the_code_host():
    assert read_kind(_dep()) == "pull_request"
    assert read_kind(_dep(merge_sha=MERGE)) is None
    assert read_kind({"repo": REPO, "kind": "deployment", "sha": MERGE, "merge_sha": MERGE}) is None
    assert read_kind({"repo": REPO, "kind": "issue", "number": 3}) == "issue"


# -- the Waits for line -------------------------------------------------------------


def _blocked(refs: list) -> SimpleNamespace:
    return SimpleNamespace(
        status="blocked",
        blocked_on=None,
        blocked_on_refs=refs,
        blocked_reason=None,
        needs_human=None,
        action_mode=None,
        not_before=None,
    )


def test_the_waits_for_line_before_the_merge_and_before_the_deploy(monkeypatch):
    monkeypatch.setattr(refs_mod, "_running_version", lambda: "1.261104.0-dev.6be4875")
    assert blocked_why(_blocked([_dep()]))["text"] == f"Waits for: deployment of {REPO}#8412 (not merged yet)"
    # Merged, never read against a build: the line says so rather than "not in build".
    assert blocked_why(_blocked([_dep(merge_sha=MERGE)]))["text"] == (
        f"Waits for: deployment of {REPO}#8412 (merged f168aa7, not checked yet against build 1.261104.0-dev.6be4875)"
    )
    assert blocked_why(_blocked([_dep(merge_sha=MERGE, build="1.2.3")]))["text"].endswith("not in build 1.2.3)")
    # Deployed: nothing left to wait for.
    assert blocked_why(_blocked([_dep(merge_sha=MERGE, state="deployed")]))["text"] == "No reason given"


def test_the_release_note_names_the_merge_and_the_build():
    note = deployed_note(_dep(merge_sha=MERGE), "1.0.1", "2026-11-04T13:30:00+00:00", NOW)
    assert note == (
        f"released: deployment of {REPO}#8412 (merged f168aa7) in build 1.0.1 at 2026-11-04T13:30:00Z; "
        "released at 2026-11-04T14:00:00Z"
    )


# -- the GitHub compare -------------------------------------------------------------


def test_the_github_compare_says_whether_the_build_carries_the_commit():
    assert contains_commit({"status": "ahead"}) is True
    assert contains_commit({"status": "identical"}) is True
    assert contains_commit({"status": "behind"}) is False
    assert contains_commit({"status": "diverged"}) is False
    assert contains_commit({"message": "Not Found"}) is None
    assert contains_commit("nope") is None


def test_github_registers_its_containment_reader():
    import app.integrations.providers.github  # noqa: F401 - registers the reader

    assert "github" in reference_state.commit_containment_readers()


# -- the deploy stage ---------------------------------------------------------------


class _Result:
    def __init__(self, row=None, item=None):
        self._row, self._item = row, item

    def one_or_none(self):
        return self._row

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
        return _Result(row=(item.user_id, list(item.blocked_on_refs or [])), item=item)

    async def commit(self):
        self.store["commits"] += 1


@pytest.fixture
def deploy(monkeypatch):
    item = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="blocked",
        blocked_on=None,
        blocked_on_refs=[_dep(merge_sha=MERGE)],
        blocked_reason=None,
        needs_human=None,
        claim_notes=[],
        description="brief",
        due_at=None,
        not_before=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=None,
        claimed_at=None,
        last_activity_at=None,
    )
    store: dict = {"item": item, "commits": 0, "reads": [], "contains": True, "published": []}

    async def token(db, provider, user_id):
        return "tok"

    async def reader(repo, commit, build, tok):
        store["reads"].append((repo, commit, build))
        return store["contains"]

    async def published(item_id, note, release, deployed):
        store["published"].append((item_id, note))

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(store))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_publish_deployed", published)
    monkeypatch.setattr(refs_mod, "_containment", {})
    monkeypatch.setattr(reference_state, "_CONTAINMENT_READERS", {"github": reader})

    async def recorded(version):
        return store.get("recorded", "")

    monkeypatch.setattr(platform_builds, "recorded_git_sha", recorded)
    return store


RELEASE = {"version": "1.0.1", "git_sha": BUILD, "first_seen_at": "2026-11-04T13:30:00+00:00"}


async def test_a_build_carrying_the_merge_releases_the_item(deploy):
    item = deploy["item"]
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) == "released"
    assert item.status == "open" and item.blocked_on_refs is None and item.claimed_by_agent_id is None
    assert item.description.startswith(f"released: deployment of {REPO}#8412 (merged f168aa7) in build 1.0.1")
    assert deploy["reads"] == [(REPO, MERGE, BUILD)]
    assert len(deploy["published"]) == 1


async def test_a_build_without_the_merge_keeps_it_blocked_and_names_the_build(deploy):
    deploy["contains"] = False
    item = deploy["item"]
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) == "noted"
    assert item.status == "blocked" and item.blocked_on_refs[0]["build"] == "1.0.1"
    assert "not in build 1.0.1" in blocked_why(item)["text"]
    # The same build again: nothing to write, and no second read (cached per merge and build).
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) is None
    assert len(deploy["reads"]) == 1 and deploy["published"] == []


async def test_an_unreadable_containment_keeps_it_blocked(deploy):
    deploy["contains"] = None
    item = deploy["item"]
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) is None
    assert item.status == "blocked" and "build" not in item.blocked_on_refs[0]


async def test_one_deployment_of_two_leaves_the_item_waiting(deploy):
    item = deploy["item"]
    item.blocked_on_refs = [_dep(merge_sha=MERGE), _dep(8414)]
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) == "noted"
    assert item.status == "blocked" and item.blocked_on_refs[0]["state"] == "deployed"
    assert item.claim_notes[-1]["text"].endswith("; still waiting")


async def test_an_open_blocked_on_item_keeps_it_blocked_when_the_deployment_lands(deploy):
    # #9714: blocked on an open item and on a deployment; the build carrying the merge
    # satisfies the reference and leaves it blocked until the item is done.
    item = deploy["item"]
    item.blocked_on = [str(uuid.uuid4())]
    assert await refs_mod._apply_deployment(item.id, RELEASE, NOW) == "noted"
    assert item.status == "blocked" and item.blocked_on_refs[0]["state"] == "deployed"
    assert item.claim_notes[-1]["text"].endswith("; still waiting")
    assert deploy["published"] == []


async def test_a_release_with_no_commit_checks_nothing(deploy):
    assert await refs_mod._apply_deployment(deploy["item"].id, {"version": "1.0.1", "git_sha": None}, NOW) is None
    assert await refs_mod.on_release_deployed({"version": "1.0.1"}) == {"released": 0, "noted": 0}
    assert deploy["reads"] == []


# -- the build's commit --------------------------------------------------------------


def test_the_same_commit_by_full_or_short_sha():
    assert refs_mod.same_commit(MERGE, MERGE.upper())
    assert refs_mod.same_commit(MERGE, MERGE[:7]) and refs_mod.same_commit(MERGE[:9], MERGE)
    assert not refs_mod.same_commit(MERGE, BUILD)
    assert not refs_mod.same_commit(MERGE, MERGE[:6]) and not refs_mod.same_commit("", "")


async def test_a_build_that_is_the_merge_commit_releases_without_a_read(deploy):
    deploy["contains"] = False  # a read would say no: equality must not ask
    item = deploy["item"]
    release = {"version": "1.261104.0-dev.f168aa7", "git_sha": MERGE, "first_seen_at": None}
    assert await refs_mod._apply_deployment(item.id, release, NOW) == "released"
    assert item.status == "open" and deploy["reads"] == []


def test_a_dev_version_names_its_commit():
    assert platform_builds.version_git_sha("1.261104.0-dev.F3D4DA9") == "f3d4da9"
    assert platform_builds.version_git_sha("1.261104.0") == ""
    assert platform_builds.version_git_sha("") == ""


async def test_the_build_commit_falls_back_to_the_recorded_row_then_the_version(deploy):
    assert await platform_builds.resolve_build_sha("1.0.1", "ABC1234") == "abc1234"
    assert await platform_builds.resolve_build_sha("1.261104.0-dev.f168aa7", None) == "f168aa7"
    deploy["recorded"] = MERGE
    assert await platform_builds.resolve_build_sha("1.261104.0-dev.f168aa7", None) == MERGE
    deploy["recorded"] = ""
    assert await platform_builds.resolve_build_sha("1.0.1", None) == ""


async def test_a_process_without_git_sha_still_checks_and_releases_at_the_write(deploy, monkeypatch):
    # The process answering has no GIT_SHA (running.git_sha null),
    # the row its sibling recorded carries the merge commit itself.
    monkeypatch.setattr(platform_builds, "running_build", lambda: ("1.261104.0-dev.f168aa7", ""))
    deploy["recorded"] = MERGE

    async def no_merge_stage(item_id):
        return None

    monkeypatch.setattr(refs_mod, "_read_merge_stage", no_merge_stage)
    item = deploy["item"]
    assert await refs_mod.running_release() == {
        "version": "1.261104.0-dev.f168aa7",
        "git_sha": MERGE,
        "first_seen_at": None,
    }
    assert await refs_mod.check_item_deployment(item.id) == "released"
    assert item.status == "open" and deploy["reads"] == []
    assert item.description.startswith(f"released: deployment of {REPO}#8412 (merged f168aa7) in build 1.261104.0-dev.f168aa7")


async def test_a_release_event_with_a_null_commit_reads_it_from_the_recorded_row(deploy, monkeypatch):
    deploy["recorded"] = BUILD
    item = deploy["item"]

    async def candidates(db):
        return [item.id]

    monkeypatch.setattr(refs_mod, "_deployment_candidates", candidates)
    assert await refs_mod.on_release_deployed({"version": "1.0.1", "git_sha": None}) == {"released": 1, "noted": 0}
    assert deploy["reads"] == [(REPO, MERGE, BUILD)]


async def test_the_release_event_checks_every_candidate_and_never_raises(monkeypatch):
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    outcomes = {ids[0]: "released", ids[1]: None}

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def candidates(db):
        return list(ids)

    async def apply(item_id, release, now):
        if item_id == ids[2]:
            raise RuntimeError("boom")
        return outcomes[item_id]

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _S())
    monkeypatch.setattr(refs_mod, "_deployment_candidates", candidates)
    monkeypatch.setattr(refs_mod, "_apply_deployment", apply)
    assert await refs_mod.on_release_deployed(RELEASE) == {"released": 1, "noted": 0}


async def test_the_polling_fallback_reads_only_the_merge_stage(monkeypatch):
    reads: list = []

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    user = uuid.uuid4()

    async def candidates(db, now):
        return [(uuid.uuid4(), user, [_dep(), _dep(9, merge_sha=MERGE)])]

    async def token(db, provider, user_id):
        return "tok"

    async def reader(repo, kind, number, tok):
        reads.append((kind, number))
        return None

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _S())
    monkeypatch.setattr(refs_mod, "_poll_candidates", candidates)
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
    await refs_mod.poll_blocked_on_refs(NOW)
    assert reads == [("pull_request", 8412)]
