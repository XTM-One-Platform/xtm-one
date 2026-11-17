"""A pull request parked on its base's red full CI runs again at the base's green (#12069).

``merge_github_pr`` parks a pull request whose full run fails on the jobs its
base's latest run fails on; a signed ``workflow_run`` completed green on the
base branch for that workflow hands every parked pull request to the
provider's redispatcher, with the parking person's credential.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.integrations.pull_request_events import PullRequestEvent
from app.services import full_ci_parking


class _Redis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttl: dict[str, int] = {}

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def delete(self, key):
        self.hashes.pop(key, None)


def _green(branch: str = "main", event: str = "push", path: str = ".github/workflows/ci.yml", prs=None) -> dict:
    return {
        "repository": {"full_name": "Acme/App"},
        "workflow_run": {
            "id": 77,
            "name": "CI",
            "workflow_id": 5,
            "path": path,
            "event": event,
            "head_branch": branch,
            "head_sha": "e" * 40,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": prs or [],
        },
    }


def test_a_push_run_green_on_main_is_a_base_green():
    event = parse_github_pull_request_event("workflow_run.completed", _green())
    assert full_ci_parking.base_green(event) == ("main", "ci.yml")


def test_a_manual_run_on_main_is_a_base_green_too():
    event = parse_github_pull_request_event("workflow_run.completed", _green(event="workflow_dispatch"))
    assert full_ci_parking.base_green(event) == ("main", "ci.yml")


def test_a_pull_request_run_or_a_red_is_no_base_green():
    pr_run = parse_github_pull_request_event("workflow_run.completed", _green(event="pull_request"))
    assert full_ci_parking.base_green(pr_run) is None
    of_a_pr = parse_github_pull_request_event("workflow_run.completed", _green(prs=[{"number": 3}]))
    assert full_ci_parking.base_green(of_a_pr) is None
    red = PullRequestEvent(
        kind="ci",
        repo="acme/app",
        head_branch="main",
        status="completed",
        conclusion="failure",
        extra={"base_run": True, "workflow_file": "ci.yml"},
    )
    assert full_ci_parking.base_green(red) is None


@pytest.mark.asyncio
async def test_park_then_base_green_redispatches_with_the_parking_persons_token():
    redis = _Redis()
    redispatcher = AsyncMock(return_value="acme/app#12: ci.yml dispatched on fix/y after update-branch")
    with (
        patch.object(full_ci_parking, "_redis", AsyncMock(return_value=redis)),
        patch("app.integrations.full_ci_redispatch._REDISPATCHERS", {"github": redispatcher}),
        patch("app.services.work_item_blocked_refs.reference_token", AsyncMock(return_value="tok-u1")),
        patch("app.core.database.async_session") as session,
    ):
        session.return_value.__aenter__ = AsyncMock(return_value=object())
        session.return_value.__aexit__ = AsyncMock(return_value=None)
        assert await full_ci_parking.park("github", "Acme/App", "main", "ci.yml", 12, "a" * 40, "u1")
        assert redis.ttl and next(iter(redis.ttl.values())) == full_ci_parking.PARK_TTL_SECONDS

        event = parse_github_pull_request_event("workflow_run.completed", _green())
        task = full_ci_parking.on_base_green("github", event)
        assert task is not None
        await task

    redispatcher.assert_awaited_once_with("acme/app", 12, "a" * 40, "ci.yml", "tok-u1")
    assert redis.hashes == {}


@pytest.mark.asyncio
async def test_another_workflow_or_branch_leaves_the_parked_pull_request_waiting():
    redis = _Redis()
    redispatcher = AsyncMock(return_value="")
    with (
        patch.object(full_ci_parking, "_redis", AsyncMock(return_value=redis)),
        patch("app.integrations.full_ci_redispatch._REDISPATCHERS", {"github": redispatcher}),
    ):
        await full_ci_parking.park("github", "acme/app", "main", "ci.yml", 12, "a" * 40, "u1")
        for payload in (_green(path=".github/workflows/e2e.yml"), _green(branch="release")):
            event = parse_github_pull_request_event("workflow_run.completed", payload)
            task = full_ci_parking.on_base_green("github", event)
            if task is not None:
                await task

    redispatcher.assert_not_awaited()
    assert len(redis.hashes) == 1


@pytest.mark.asyncio
async def test_park_without_redis_is_not_recorded():
    with patch.object(full_ci_parking, "_redis", AsyncMock(return_value=None)):
        assert not await full_ci_parking.park("github", "acme/app", "main", "ci.yml", 12, "a" * 40, "u1")
