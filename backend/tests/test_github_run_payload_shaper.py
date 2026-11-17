"""A GitHub push is shown to the run trimmed: no duplicated head commit, first-line messages, no URL templates.

A squash merge of a pull request with a long body delivered 174,383 characters
(two copies of the body, plus every ``*_url`` template), cut at the 150,000
cap of the serialiser. The run acts on the repository, the ref, before/after
and, per commit, its id, first line and paths: those are kept whole.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import app.integrations.providers.github  # noqa: F401  (registers the shaper)
from app.integrations.providers.github.run_payload import (
    MESSAGE_FIRST_LINE_MAX_CHARS,
    shape_github_run_payload,
)
from app.integrations.run_payload_shapers import (
    register_run_payload_shaper,
    run_payload_shaper,
    shape_run_payload,
)
from app.services.assignment_helpers import _build_trigger_information

_BODY = "feat(dashboards): studio (#6601)\n\n" + ("A long pull request body line. " * 2_800)
_URLS = {f"{name}_url": f"https://api.github.com/repos/o/r/{name}{{/id}}" for name in (
    "archive", "assignees", "blobs", "branches", "collaborators", "comments", "commits", "compare",
    "contents", "contributors", "deployments", "downloads", "events", "forks", "git_commits", "git_refs",
    "git_tags", "hooks", "issue_comment", "issue_events", "issues", "keys", "labels", "languages",
    "merges", "milestones", "notifications", "pulls", "releases", "stargazers", "statuses",
    "subscribers", "subscription", "tags", "teams", "trees",
)}


def _commit(sha: str, message: str, added: list[str]) -> dict:
    return {
        "id": sha,
        "tree_id": "t" * 40,
        "distinct": True,
        "message": message,
        "timestamp": "2026-11-05T13:30:00Z",
        "url": f"https://github.com/o/r/commit/{sha}",
        "author": {"name": "A", "email": "a@example.com", "username": "a"},
        "committer": {"name": "GitHub", "email": "noreply@github.com", "username": "web-flow"},
        "added": added,
        "removed": ["backend/old.py"],
        "modified": ["backend/app/main.py"],
    }


def _push() -> dict:
    added = [f"backend/alembic/versions/d10mrk659{i:04d}_dashboards.py" for i in range(10)]
    head = _commit("b" * 40, _BODY, added)
    return {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": "https://github.com/o/r/compare/aaaaaaa...bbbbbbb",
        "commits": [_commit("c" * 40, "chore: bump\n\nbody", ["x.py"]), head],
        "head_commit": copy.deepcopy(head),
        "repository": {
            "id": 1,
            "full_name": "XTM-One-Platform/xtm-one",
            "html_url": "https://github.com/XTM-One-Platform/xtm-one",
            "owner": {"login": "XTM-One-Platform", "avatar_url": "x", "followers_url": "y", "html_url": "z"},
            **_URLS,
        },
        "pusher": {"name": "SamuelHassine", "email": "s@example.com"},
        "sender": {"login": "SamuelHassine", "avatar_url": "x", "repos_url": "y", "html_url": "z"},
    }


def test_push_drops_duplicate_head_commit_cuts_messages_and_url_templates():
    payload = _push()
    original = copy.deepcopy(payload)
    shaped = shape_github_run_payload("push", payload)

    assert payload == original  # the delivery itself is untouched
    assert "head_commit" not in shaped
    assert shaped["after"] == "b" * 40
    assert shaped["ref"] == "refs/heads/main"
    assert shaped["repository"]["full_name"] == "XTM-One-Platform/xtm-one"
    assert shaped["repository"]["html_url"] == "https://github.com/XTM-One-Platform/xtm-one"
    assert shaped["compare"] == original["compare"]
    assert not any(k.endswith("_url") and k != "html_url" for k in shaped["repository"])
    assert "avatar_url" not in shaped["sender"]

    last = shaped["commits"][-1]
    assert last["message"] == "feat(dashboards): studio (#6601)"
    assert last["message_truncated"] is True
    assert last["added"] == original["commits"][-1]["added"]
    assert last["removed"] == ["backend/old.py"]
    assert last["modified"] == ["backend/app/main.py"]
    assert shaped["commits"][0]["message"] == "chore: bump"


def test_squash_of_a_long_pull_request_fits_under_20000_characters():
    payload = _push()
    assert len(json.dumps(payload)) > 150_000
    shaped = shape_github_run_payload("push", payload)
    assert len(json.dumps(shaped, indent=2)) < 20_000


def test_first_line_is_capped():
    payload = _push()
    payload["commits"][0]["message"] = "x" * 1_000
    shaped = shape_github_run_payload("push", payload)
    assert len(shaped["commits"][0]["message"]) == MESSAGE_FIRST_LINE_MAX_CHARS
    assert shaped["commits"][0]["message_truncated"] is True


def test_head_commit_alone_is_kept_and_shaped():
    payload = _push()
    payload["commits"] = []
    shaped = shape_github_run_payload("push", payload)
    assert shaped["head_commit"]["message"] == "feat(dashboards): studio (#6601)"
    assert shaped["head_commit"]["added"] == payload["head_commit"]["added"]


def test_other_events_are_unchanged():
    payload = {"action": "opened", "pull_request": {"body": "x" * 10, "url": "u", "diff_url": "d"}}
    assert shape_github_run_payload("pull_request.opened", payload) is payload


def test_generic_layer_without_a_shaper_or_on_failure_shows_the_payload_whole():
    payload = {"a": 1}
    assert shape_run_payload("no-such-provider", "push", payload) is payload
    assert shape_run_payload(None, "push", payload) is payload

    def boom(event_key: str, data: dict) -> dict:
        raise RuntimeError("boom")

    register_run_payload_shaper("test-boom", boom)
    assert run_payload_shaper("test-boom") is boom
    assert shape_run_payload("test-boom", "push", payload) is payload


def test_trigger_information_shows_the_shaped_push():
    run = SimpleNamespace(
        trigger_context={"type": "webhook", "provider": "github", "event_key": "push"},
        trigger_payload={**_push(), "_webhook_meta": {"assignment_id": "x"}},
    )
    block = _build_trigger_information(run)
    assert "TRUNCATED" not in block
    assert "A long pull request body line." not in block
    assert "d10mrk6590009_dashboards.py" in block
    assert len(block) < 25_000
