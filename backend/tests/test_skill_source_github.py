"""Unit tests for ``app.services.skill_source`` — private GitHub skill sources.

Covers the two pieces of logic a private-repository skill source depends on:

1. **URL parsing** — every shape a user realistically pastes into the skill URL
   field must resolve to ``(owner, repo, path, ref)``, and anything that is not
   a GitHub *file* URL must resolve to ``None`` so public skills keep taking
   the unauthenticated path unchanged.
2. **Failure classification** — GitHub overloads 403 (forbidden *and* rate
   limit spent) and 404 (missing *and* invisible-to-your-token), and the
   refresh scheduler's back-off / quarantine decision hangs entirely on
   telling those apart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.skill_source import (
    GitHubSkillSource,
    classify_github_failure,
    contents_api_url,
    diagnose_not_found,
    parse_github_skill_url,
    repo_api_url,
)

# ---------------------------------------------------------------------------
# URL parsing — recognised shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Web UI "blob" link — the form a user copies from the GitHub file view.
        (
            "https://github.com/sopra/agent-skills/blob/main/skills/triage/SKILL.md",
            ("sopra", "agent-skills", "skills/triage/SKILL.md", "main"),
        ),
        # /raw/ and /blame/ address the same file as /blob/.
        (
            "https://github.com/sopra/agent-skills/raw/main/SKILL.md",
            ("sopra", "agent-skills", "SKILL.md", "main"),
        ),
        (
            "https://github.com/sopra/agent-skills/blame/develop/a/b/SKILL.md",
            ("sopra", "agent-skills", "a/b/SKILL.md", "develop"),
        ),
        # raw.githubusercontent.com — short form.
        (
            "https://raw.githubusercontent.com/sopra/agent-skills/main/SKILL.md",
            ("sopra", "agent-skills", "SKILL.md", "main"),
        ),
        # raw.githubusercontent.com — fully-qualified ref form.
        (
            "https://raw.githubusercontent.com/sopra/agent-skills/refs/heads/main/x/SKILL.md",
            ("sopra", "agent-skills", "x/SKILL.md", "main"),
        ),
        # A tag ref namespace resolves the same way.
        (
            "https://raw.githubusercontent.com/sopra/agent-skills/refs/tags/v1.2/SKILL.md",
            ("sopra", "agent-skills", "SKILL.md", "v1.2"),
        ),
        # Contents API URL with an explicit ref.
        (
            "https://api.github.com/repos/sopra/agent-skills/contents/skills/SKILL.md?ref=main",
            ("sopra", "agent-skills", "skills/SKILL.md", "main"),
        ),
        # Contents API URL without a ref — ref stays None (= default branch).
        (
            "https://api.github.com/repos/sopra/agent-skills/contents/SKILL.md",
            ("sopra", "agent-skills", "SKILL.md", None),
        ),
        # A ``.git`` suffix pasted from a clone URL must not break the API path.
        (
            "https://github.com/sopra/agent-skills.git/blob/main/SKILL.md",
            ("sopra", "agent-skills", "SKILL.md", "main"),
        ),
    ],
)
def test_parse_github_skill_url_recognised_shapes(url, expected):
    source = parse_github_skill_url(url)
    assert source is not None, f"expected {url} to be recognised as a GitHub file URL"
    assert (source.owner, source.repo, source.path, source.ref) == expected


# ---------------------------------------------------------------------------
# URL parsing — everything else falls back to the anonymous path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        # Not GitHub at all — a public SKILL.md on any other host.
        "https://example.com/skills/SKILL.md",
        # GitHub Enterprise Server is explicitly out of scope.
        "https://github.mycorp.local/sopra/agent-skills/blob/main/SKILL.md",
        # Repository root / no file path.
        "https://github.com/sopra/agent-skills",
        "https://github.com/sopra/agent-skills/blob/main",
        # A directory listing, not a file — the contents API would answer with a
        # JSON listing that must never be cached as the skill body.
        "https://github.com/sopra/agent-skills/tree/main/skills",
        # Gists are a different API surface.
        "https://gist.github.com/someone/deadbeef",
        # Non-http scheme.
        "ftp://github.com/sopra/agent-skills/blob/main/SKILL.md",
        # Contents API URL missing the /contents/ segment.
        "https://api.github.com/repos/sopra/agent-skills/commits/main",
    ],
)
def test_parse_github_skill_url_unrecognised_shapes(url):
    assert parse_github_skill_url(url) is None


def test_parse_github_skill_url_ref_override_disambiguates_slashed_branch():
    """A branch name containing ``/`` is only resolvable with an explicit ref.

    ``/blob/release/2026-07/docs/SKILL.md`` is genuinely ambiguous: without a
    hint the first segment wins (``release``), and the explicit override is the
    only way to say "the branch is ``release/2026-07``".
    """
    url = "https://github.com/sopra/agent-skills/blob/release/2026-07/docs/SKILL.md"

    without_hint = parse_github_skill_url(url)
    assert without_hint is not None
    assert without_hint.ref == "release"
    assert without_hint.path == "2026-07/docs/SKILL.md"

    with_hint = parse_github_skill_url(url, ref_override="release/2026-07")
    assert with_hint is not None
    assert with_hint.ref == "release/2026-07"
    assert with_hint.path == "docs/SKILL.md"


def test_parse_github_skill_url_ref_override_matches_fully_qualified_form():
    """An override also matches through a ``refs/heads/`` prefix."""
    url = (
        "https://raw.githubusercontent.com/sopra/agent-skills"
        "/refs/heads/release/2026-07/docs/SKILL.md"
    )
    source = parse_github_skill_url(url, ref_override="release/2026-07")
    assert source is not None
    assert source.ref == "release/2026-07"
    assert source.path == "docs/SKILL.md"


def test_parse_github_skill_url_ignores_non_matching_override():
    """An override that does not prefix the segments must not corrupt the path."""
    url = "https://github.com/sopra/agent-skills/blob/main/docs/SKILL.md"
    source = parse_github_skill_url(url, ref_override="some-other-branch")
    assert source is not None
    assert source.ref == "main"
    assert source.path == "docs/SKILL.md"


# ---------------------------------------------------------------------------
# Contents API URL construction
# ---------------------------------------------------------------------------


def test_contents_api_url_uses_ref_from_url():
    source = GitHubSkillSource(
        owner="sopra", repo="agent-skills", path="skills/SKILL.md", ref="main"
    )
    assert contents_api_url(source) == (
        "https://api.github.com/repos/sopra/agent-skills/contents/skills/SKILL.md?ref=main"
    )


def test_contents_api_url_explicit_ref_wins():
    """``AgentSkill.source_ref`` overrides whatever the URL carried."""
    source = GitHubSkillSource(
        owner="sopra", repo="agent-skills", path="SKILL.md", ref="main"
    )
    assert contents_api_url(source, ref="develop").endswith("?ref=develop")


def test_contents_api_url_omits_ref_for_default_branch_tracking():
    """No ref anywhere -> no ``ref`` parameter -> GitHub serves default-branch HEAD.

    This is the "always the latest merged version" contract: the default branch
    is resolved by GitHub at fetch time, so no extra API round-trip is spent
    looking up its name and no commit is ever implicitly pinned.
    """
    source = GitHubSkillSource(owner="sopra", repo="agent-skills", path="SKILL.md")
    url = contents_api_url(source)
    assert url == "https://api.github.com/repos/sopra/agent-skills/contents/SKILL.md"
    assert "ref=" not in url


def test_contents_api_url_encodes_path_but_keeps_separators():
    source = GitHubSkillSource(
        owner="sopra", repo="agent-skills", path="my skills/a?b/SKILL.md", ref="main"
    )
    url = contents_api_url(source)
    assert "/contents/my%20skills/a%3Fb/SKILL.md?ref=main" in url


def test_repo_api_url():
    source = GitHubSkillSource(owner="sopra", repo="agent-skills", path="SKILL.md")
    assert repo_api_url(source) == "https://api.github.com/repos/sopra/agent-skills"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        # Bad / expired credentials.
        (401, {}, "auth"),
        (403, {}, "auth"),
        # 403 doubles as "rate limit spent" — must NOT be blamed on the token.
        (403, {"x-ratelimit-remaining": "0"}, "rate_limit"),
        (403, {"X-RateLimit-Remaining": "0"}, "rate_limit"),
        (403, {"retry-after": "60"}, "rate_limit"),
        # A 403 with budget left really is a permission problem.
        (403, {"x-ratelimit-remaining": "4999"}, "auth"),
        # Explicit throttling / timeout.
        (429, {}, "rate_limit"),
        (408, {}, "rate_limit"),
        # Missing resource.
        (404, {}, "not_found"),
        # Provider-side failure.
        (500, {}, "transient"),
        (502, {}, "transient"),
        # Any other client error.
        (400, {}, "client"),
        (422, {}, "client"),
    ],
)
def test_classify_github_failure(status, headers, expected):
    assert classify_github_failure(status, headers) == expected


def test_classify_github_failure_tolerates_missing_headers():
    assert classify_github_failure(401, None) == "auth"


# ---------------------------------------------------------------------------
# 404 diagnosis — "path is gone" vs "the token cannot see this repository"
# ---------------------------------------------------------------------------


def _client_returning(status_code: int) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


_SOURCE = GitHubSkillSource(owner="sopra", repo="agent-skills", path="SKILL.md")


@pytest.mark.asyncio
async def test_diagnose_not_found_repo_readable_means_path_is_gone():
    """Repo readable -> the file really is missing, so quarantine applies."""
    client = _client_returning(200)
    assert await diagnose_not_found(client, _SOURCE, {}) == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_diagnose_not_found_repo_forbidden_means_credential(status):
    """Repo rejected -> the credential is the problem, never quarantine."""
    client = _client_returning(status)
    assert await diagnose_not_found(client, _SOURCE, {}) == "auth"


@pytest.mark.asyncio
async def test_diagnose_not_found_repo_also_404_stays_not_found():
    """Repo invisible too: deleted vs access-revoked is genuinely ambiguous.

    Keeping ``not_found`` preserves the quarantine — a repository that was actually
    deleted must still be quarantined rather than retried forever.
    """
    client = _client_returning(404)
    assert await diagnose_not_found(client, _SOURCE, {}) == "not_found"


@pytest.mark.asyncio
async def test_diagnose_not_found_probe_failure_degrades_safely():
    """A failed probe must never raise out of the diagnosis path."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=RuntimeError("boom"))
    assert await diagnose_not_found(client, _SOURCE, {}) == "not_found"
