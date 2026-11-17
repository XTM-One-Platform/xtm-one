"""Auto-refresh of skills sourced from a private GitHub repository.

``_refresh_stale_skills`` used to issue one unauthenticated ``GET`` per skill,
which can never read a ``SKILL.md`` inside a private repository — GitHub answers
401 or 404 to an anonymous request, and the quarantine then disabled the
skill as if its URL were permanently broken.

These tests pin the four behaviours that make the private-repo path correct:

1. a bound GitHub integration turns the fetch into an authenticated contents-API
   read that tracks the requested branch's HEAD;
2. an authentication failure advances a *separate* counter, logs distinctly, and
   never quarantines the skill;
3. GitHub's overloaded 403 / 404 statuses are classified before the back-off is
   chosen; and
4. skills with no credential behave exactly as they did before this feature.

The credential itself must never reach a log record — asserted throughout.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from app.services.autonomous import _refresh_stale_skills
from app.services.skill_source import SkillCredential, SkillCredentialError

TOKEN = "ghp_PRIVATEREPOTOKEN0987654321"
INTEGRATION_ID = uuid4()
BLOB_URL = "https://github.com/sopra/agent-skills/blob/main/skills/triage/SKILL.md"
CONTENTS_URL = (
    "https://api.github.com/repos/sopra/agent-skills/contents/"
    "skills/triage/SKILL.md?ref=main"
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, skills):
        self._skills = skills
        self.commits = 0

    async def execute(self, stmt):
        return _ScalarsResult(self._skills)

    async def commit(self):
        self.commits += 1


def _skill(
    *,
    url: str = BLOB_URL,
    name: str = "triage",
    source_integration_id=INTEGRATION_ID,
    source_ref: str | None = None,
    refresh_failure_count: int = 0,
    auth_failure_count: int = 0,
):
    return SimpleNamespace(
        id="skill-1",
        url=url,
        name=name,
        content=None,
        fetched_at=None,
        enabled=True,
        description=None,
        compatibility=None,
        refresh_failure_count=refresh_failure_count,
        auth_failure_count=auth_failure_count,
        source_integration_id=source_integration_id,
        source_ref=source_ref,
    )


def _credential() -> SkillCredential:
    return SkillCredential(
        token=TOKEN,
        integration_id=INTEGRATION_ID,
        integration_name="Sopra GitHub PAT",
    )


def _ok_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    resp.text = text
    return resp


def _error_response(status: int, reason: str, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.reason_phrase = reason
    resp.headers = headers or {}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"Client error '{status} {reason}'",
        request=MagicMock(),
        response=resp,
    )
    return resp


class _FakeAsyncClient:
    """Explicit async context-manager shim wrapping a recording ``get``.

    Deliberately a real class rather than an ``AsyncMock`` with ``__aenter__``
    assigned on the instance: special-method lookup goes through the type, so
    that assignment depends on mock's magic-method machinery, and when it does
    not reach the type ``async with`` silently binds a *different* auto-created
    mock — the request then lands on an object the test never inspects, and a
    "no request was made" bug reads as an unrelated ``call_args is None``.
    ``get`` stays an ``AsyncMock`` because it is called directly (no dunder
    involved), which keeps ``side_effect`` sequences and ``await_count`` usable.
    """

    def __init__(self, get: AsyncMock):
        self.get = get

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


def _client(get: AsyncMock) -> _FakeAsyncClient:
    return _FakeAsyncClient(get)


async def _refresh(db, client, *, credential_result=None, credential_error=None):
    """Run one refresh pass with a stubbed credential resolver."""
    if credential_error is not None:
        resolver = AsyncMock(side_effect=credential_error)
    else:
        resolver = AsyncMock(return_value=credential_result or _credential())
    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=client),
        patch("app.services.skill_source.resolve_skill_credential", resolver),
    ):
        return await _refresh_stale_skills(db)


# ---------------------------------------------------------------------------
# 1. Authenticated success — AC 1 and AC 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_refresh_reads_contents_api_with_bearer_token():
    """A bound credential rewrites the fetch to the authenticated contents API."""
    skill = _skill()
    db = _FakeDB([skill])
    get = AsyncMock(
        return_value=_ok_response(
            "---\nname: triage-v2\ndescription: merged desc\n"
            "compatibility: filigran-copilot\n---\n# body"
        )
    )

    refreshed = await _refresh(db, _client(get))

    assert refreshed == 1
    request_url, kwargs = get.call_args[0][0], get.call_args[1]
    assert request_url == CONTENTS_URL, (
        "an authenticated fetch must go through the contents API, not the web URL"
    )
    headers = kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github.v3.raw", (
        "the raw media type is required so the response body is the file itself"
    )


@pytest.mark.asyncio
async def test_authenticated_refresh_updates_content_and_frontmatter_metadata():
    """Success updates content, fetched_at and frontmatter-derived metadata.

    This is the "next PR merged to the tracked branch is picked up within one
    refresh cycle" acceptance criterion: the branch-HEAD read returns the new
    body and every derived field follows it.
    """
    skill = _skill(refresh_failure_count=2, auth_failure_count=2)
    db = _FakeDB([skill])
    raw = (
        "---\nname: triage-v2\ndescription: merged desc\n"
        "compatibility: filigran-copilot\n---\n# new body"
    )

    await _refresh(db, _client(AsyncMock(return_value=_ok_response(raw))))

    assert skill.content == raw
    assert skill.name == "triage-v2"
    assert skill.description == "merged desc"
    assert skill.compatibility == "filigran-copilot"
    assert skill.fetched_at is not None
    assert skill.refresh_failure_count == 0
    assert skill.auth_failure_count == 0, (
        "a successful authenticated refresh must clear the auth-failure counter"
    )


@pytest.mark.asyncio
async def test_source_ref_omitted_tracks_default_branch_head():
    """No ref in the URL and none pinned -> no ``ref`` param -> default-branch HEAD."""
    skill = _skill(
        url="https://api.github.com/repos/sopra/agent-skills/contents/SKILL.md"
    )
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_ok_response("# body"))

    await _refresh(db, _client(get))

    assert get.call_args[0][0] == (
        "https://api.github.com/repos/sopra/agent-skills/contents/SKILL.md"
    )
    assert "ref=" not in get.call_args[0][0]


@pytest.mark.asyncio
async def test_source_ref_override_wins_over_url_ref():
    """An explicit ``source_ref`` follows a non-default branch."""
    skill = _skill(source_ref="develop")
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_ok_response("# body"))

    await _refresh(db, _client(get))

    assert get.call_args[0][0].endswith("?ref=develop")


@pytest.mark.asyncio
async def test_successful_authenticated_refresh_never_logs_the_token(caplog):
    skill = _skill()
    db = _FakeDB([skill])
    with caplog.at_level(logging.DEBUG, logger="app.services.autonomous"):
        await _refresh(db, _client(AsyncMock(return_value=_ok_response("# body"))))

    combined = "\n".join(caplog.messages)
    assert TOKEN not in combined
    assert "authenticated fetch" in combined, (
        "the success log must record whether the fetch was authenticated"
    )


# ---------------------------------------------------------------------------
# 2. Authentication failures — AC 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_with_credential_increments_auth_counter_only(caplog):
    """An expired credential must not be mistaken for a permanently broken URL."""
    skill = _skill(refresh_failure_count=0, auth_failure_count=0)
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(401, "Unauthorized"))

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        refreshed = await _refresh(db, _client(get))

    assert refreshed == 0
    assert skill.auth_failure_count == 1
    assert skill.refresh_failure_count == 0, (
        "an auth failure must never advance the quarantine counter"
    )
    combined = "\n".join(caplog.messages)
    assert "Authentication failure" in combined
    assert "NOT quarantined" in combined
    assert TOKEN not in combined


@pytest.mark.asyncio
async def test_403_without_rate_limit_headers_is_an_auth_failure():
    skill = _skill()
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(403, "Forbidden"))

    await _refresh(db, _client(get))

    assert skill.auth_failure_count == 1
    assert skill.refresh_failure_count == 0


@pytest.mark.asyncio
async def test_auth_failure_never_quarantines_even_past_the_threshold(caplog):
    """Repeated auth failures raise an alert but leave the skill enabled.

    A quarantine would report "this URL is dead" when the real fix is rotating
    a token, so the skill keeps its daily retry and a rotated credential heals
    it with no action on the skill itself.
    """
    skill = _skill(auth_failure_count=2)
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(401, "Unauthorized"))

    with (
        patch("app.core.config.settings.skill_refresh_auth_fail_alert_threshold", 3),
        caplog.at_level(logging.ERROR, logger="app.services.autonomous"),
    ):
        await _refresh(db, _client(get))

    assert skill.auth_failure_count == 3
    assert skill.refresh_failure_count == 0
    assert skill.enabled is True, "an auth failure must never disable the skill"
    combined = "\n".join(caplog.messages)
    assert "failed authentication 3 times" in combined
    assert "still enabled" in combined
    assert "quarantin" not in combined.lower(), (
        "the auth alert must not reuse the quarantine wording"
    )


@pytest.mark.asyncio
async def test_auth_failure_keeps_the_daily_retry_cadence():
    """``fetched_at`` moves to now, so the retry is daily rather than hourly."""
    from datetime import UTC, datetime, timedelta

    skill = _skill()
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(401, "Unauthorized"))

    await _refresh(db, _client(get))

    assert skill.fetched_at is not None
    assert skill.fetched_at > datetime.now(UTC) - timedelta(minutes=5), (
        "an expired token will not fix itself within the hour — do not back off "
        "to the short transient window"
    )


@pytest.mark.asyncio
async def test_unusable_credential_is_reported_without_any_http_request(caplog):
    """A broken integration binding fails before a request is ever attempted."""
    skill = _skill()
    db = _FakeDB([skill])
    get = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        await _refresh(
            db,
            _client(get),
            credential_error=SkillCredentialError(
                "GitHub integration 'Sopra GitHub PAT' is not connected "
                "(status: disconnected). Re-authorize it in Integrations."
            ),
        )

    get.assert_not_awaited()
    assert skill.auth_failure_count == 1
    assert skill.refresh_failure_count == 0
    assert "is not connected" in "\n".join(caplog.messages)


@pytest.mark.asyncio
async def test_credential_bound_to_non_github_url_is_refused_without_sending_token(caplog):
    """A bearer token must never be attached to an unintended origin."""
    skill = _skill(url="https://example.com/skills/SKILL.md")
    db = _FakeDB([skill])
    get = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        await _refresh(db, _client(get))

    get.assert_not_awaited()
    assert skill.auth_failure_count == 1
    combined = "\n".join(caplog.messages)
    assert "not a GitHub repository file URL" in combined
    assert TOKEN not in combined


# ---------------------------------------------------------------------------
# 3. GitHub's overloaded status codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_403_rate_limit_is_transient_not_an_auth_failure():
    """A spent rate limit must not be blamed on the operator's credential."""
    from datetime import UTC, datetime, timedelta

    skill = _skill(refresh_failure_count=1, auth_failure_count=1)
    db = _FakeDB([skill])
    get = AsyncMock(
        return_value=_error_response(
            403, "Forbidden", headers={"x-ratelimit-remaining": "0"}
        )
    )

    await _refresh(db, _client(get))

    assert skill.auth_failure_count == 1, "a throttle must not advance the auth counter"
    assert skill.refresh_failure_count == 1, "a throttle must not advance quarantine"
    assert skill.fetched_at < datetime.now(UTC) - timedelta(hours=22), (
        "a throttle gets the short transient back-off"
    )


@pytest.mark.asyncio
async def test_404_with_readable_repo_still_quarantines(caplog):
    """Repo readable but file missing -> a genuinely broken path."""
    skill = _skill(refresh_failure_count=2)
    db = _FakeDB([skill])
    file_404 = _error_response(404, "Not Found")
    repo_ok = MagicMock(status_code=200, headers={})
    get = AsyncMock(side_effect=[file_404, repo_ok])

    with (
        patch("app.core.config.settings.skill_refresh_quarantine_threshold", 3),
        caplog.at_level(logging.ERROR, logger="app.services.autonomous"),
    ):
        await _refresh(db, _client(get))

    assert skill.refresh_failure_count == 3
    assert skill.auth_failure_count == 0
    assert "Quarantining skill" in "\n".join(caplog.messages)


@pytest.mark.asyncio
async def test_404_with_forbidden_repo_is_an_auth_failure(caplog):
    """GitHub hides invisible repos behind 404 — the probe unmasks the real cause."""
    skill = _skill(refresh_failure_count=2)
    db = _FakeDB([skill])
    file_404 = _error_response(404, "Not Found")
    repo_403 = MagicMock(status_code=403, headers={})
    get = AsyncMock(side_effect=[file_404, repo_403])

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        await _refresh(db, _client(get))

    assert skill.auth_failure_count == 1
    assert skill.refresh_failure_count == 2, (
        "a credential-scope problem must not push the skill toward quarantine"
    )
    assert "Authentication failure" in "\n".join(caplog.messages)


@pytest.mark.asyncio
async def test_5xx_on_authenticated_fetch_is_transient():
    from datetime import UTC, datetime, timedelta

    skill = _skill(refresh_failure_count=1)
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(503, "Service Unavailable"))

    await _refresh(db, _client(get))

    assert skill.refresh_failure_count == 1
    assert skill.auth_failure_count == 0
    assert skill.fetched_at < datetime.now(UTC) - timedelta(hours=22)


# ---------------------------------------------------------------------------
# 4. No regression for unauthenticated skills — AC 4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_skill_without_credential_uses_a_plain_get():
    """No bound integration -> the original anonymous request, unchanged."""
    skill = _skill(
        url="https://raw.githubusercontent.com/public/repo/main/SKILL.md",
        source_integration_id=None,
    )
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_ok_response("# body"))

    await _refresh(db, _client(get))

    assert get.call_args[0][0] == (
        "https://raw.githubusercontent.com/public/repo/main/SKILL.md"
    ), "a public skill must keep fetching its own URL, not the contents API"
    assert get.call_args[1]["headers"] is None


@pytest.mark.asyncio
async def test_401_without_credential_keeps_the_pre_2628_behaviour():
    """A private URL with no credential fails exactly as it does today.

    Without a credential there is nothing to rotate, so 401 stays a permanent
    client-side failure that counts toward quarantine — the explicit
    no-regression requirement.
    """
    skill = _skill(
        url="https://raw.githubusercontent.com/sopra/private/main/SKILL.md",
        source_integration_id=None,
    )
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(401, "Unauthorized"))

    await _refresh(db, _client(get))

    assert skill.refresh_failure_count == 1
    assert skill.auth_failure_count == 0
    assert get.await_count == 1, "no repo probe is issued on the anonymous path"


@pytest.mark.asyncio
async def test_404_without_credential_does_not_probe_the_repo(caplog):
    """No credential means nothing to reinterpret: a 404 is taken at face value.

    The repo probe exists only to unmask "your token cannot see this repo"; on
    an anonymous read there is no token, so the extra request would be waste.
    The outcome is still labelled ``not_found`` rather than a generic ``client``
    so the anonymous and authenticated paths report the same failure kinds.
    """
    skill = _skill(
        url="https://raw.githubusercontent.com/public/repo/main/SKILL.md",
        source_integration_id=None,
    )
    db = _FakeDB([skill])
    get = AsyncMock(return_value=_error_response(404, "Not Found"))

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        await _refresh(db, _client(get))

    assert skill.refresh_failure_count == 1
    assert get.await_count == 1
    assert "[not_found]" in "\n".join(caplog.messages)


@pytest.mark.asyncio
async def test_one_credential_serves_several_skills_and_resolves_once():
    """Several skills may share one integration — a repo usually hosts several.

    The credential is memoised for the batch, so a repository with N skills
    costs one lookup and one decrypt instead of N.
    """
    skills = [
        _skill(name="triage", url=BLOB_URL),
        _skill(
            name="review",
            url="https://github.com/sopra/agent-skills/blob/main/skills/review/SKILL.md",
        ),
        _skill(
            name="digest",
            url="https://github.com/sopra/agent-skills/blob/main/skills/digest/SKILL.md",
        ),
    ]
    db = _FakeDB(skills)
    get = AsyncMock(return_value=_ok_response("# body"))
    resolver = AsyncMock(return_value=_credential())

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=_client(get)),
        patch("app.services.skill_source.resolve_skill_credential", resolver),
    ):
        refreshed = await _refresh_stale_skills(db)

    assert refreshed == 3
    assert resolver.await_count == 1, (
        "skills sharing an integration must not each re-resolve the credential"
    )
    assert get.await_count == 3


@pytest.mark.asyncio
async def test_mixed_batch_isolates_failures():
    """One skill's auth failure must not abort the rest of the batch."""
    private = _skill(name="private-skill")
    public = _skill(
        name="public-skill",
        url="https://raw.githubusercontent.com/public/repo/main/SKILL.md",
        source_integration_id=None,
    )
    db = _FakeDB([private, public])

    async def _dispatch(url, headers=None):
        if url.startswith("https://api.github.com"):
            return _error_response(401, "Unauthorized")
        return _ok_response("---\nname: public-skill\n---\n# body")

    refreshed = await _refresh(db, _client(AsyncMock(side_effect=_dispatch)))

    assert refreshed == 1
    assert private.auth_failure_count == 1
    assert public.content is not None
