"""Target extraction for GitHub self-action echo suppression.

``extract_github_target`` is the pure, Redis-free half of the loop-protection
mechanism: it maps a raw GitHub webhook payload to the ``(repo, number)`` the
event concerns so the webhook receiver can ask whether the agent just wrote to
that same target.  These tests pin the event families an assignment typically
subscribes to (issues / issue_comment / pull_request) and confirm non-scoped
payloads (push, star, malformed) yield ``None`` (no suppression applied).
"""

from __future__ import annotations

import uuid

from app.services.github_echo import _key, extract_github_target


def test_issues_event():
    payload = {
        "action": "edited",
        "repository": {"full_name": "OpenCTI-Platform/connectors"},
        "issue": {"number": 1443},
        "sender": {"login": "SamuelHassine"},
    }
    assert extract_github_target(payload) == ("OpenCTI-Platform/connectors", 1443)


def test_issue_comment_event_uses_issue_number():
    payload = {
        "action": "created",
        "repository": {"full_name": "filigran/xtm-one"},
        "issue": {"number": 77},
        "comment": {"id": 999},
    }
    assert extract_github_target(payload) == ("filigran/xtm-one", 77)


def test_pull_request_event():
    payload = {
        "action": "opened",
        "repository": {"full_name": "filigran/xtm-one"},
        "pull_request": {"number": 42},
    }
    assert extract_github_target(payload) == ("filigran/xtm-one", 42)


def test_pull_request_review_top_level_number():
    payload = {
        "action": "submitted",
        "repository": {"full_name": "filigran/xtm-one"},
        "number": 42,
        "pull_request": {"id": 123},
    }
    assert extract_github_target(payload) == ("filigran/xtm-one", 42)


def test_push_event_has_no_target():
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "filigran/xtm-one"},
    }
    assert extract_github_target(payload) is None


def test_missing_repository_returns_none():
    assert extract_github_target({"issue": {"number": 1}}) is None


def test_non_dict_returns_none():
    assert extract_github_target(None) is None
    assert extract_github_target("not a dict") is None  # type: ignore[arg-type]


def test_key_canonicalizes_integration_id():
    """Different spellings of the same integration UUID must map to one Redis
    key, so the recorder (bound to the agent's binding id) and the webhook
    checker (the canonical URL-path id) can never miss each other because the
    two sides spelled the id differently.
    """
    canonical = str(uuid.uuid4())
    assert _key(canonical, "owner/repo", 5) == _key(
        canonical.upper(), "owner/repo", 5
    )
    assert _key(canonical, "owner/repo", 5) == _key(
        canonical.replace("-", ""), "owner/repo", 5
    )


def test_key_normalizes_repo_and_non_uuid_id():
    """Repo casing is normalized, and a non-UUID id still yields a stable key."""
    canonical = str(uuid.uuid4())
    assert _key(canonical, "Owner/Repo", 5) == _key(canonical, "owner/repo", 5)
    assert _key("Not-A-UUID", "owner/repo", 5) == _key("not-a-uuid", "owner/repo", 5)
