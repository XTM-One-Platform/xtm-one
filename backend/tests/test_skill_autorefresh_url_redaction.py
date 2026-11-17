"""Regression tests for credential redaction in ``_refresh_stale_skills`` log output.

Skills with ``token=`` query-string credentials in their source URLs were leaking
those tokens to application logs on every daily refresh failure.

The fix wraps every URL logged in ``_refresh_stale_skills`` with
``_redact_url_for_log`` and passes exception messages through
``_redact_urls_in_text`` so that ``token=`` (and any other query-string
parameter that could carry a credential) is stripped before reaching the
log record.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.autonomous import _refresh_stale_skills

# ---------------------------------------------------------------------------
# Helpers
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


def _skill(url: str, name: str = "Test Skill", refresh_failure_count: int = 0):
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
    )


# ---------------------------------------------------------------------------
# 4xx failure — token must not appear in warning log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_failure_redacts_token_from_url_in_warning(caplog):
    """A 404 response for a URL with ``token=`` must not log the token value."""
    url_with_token = (
        "https://raw.githubusercontent.com/FiligranHQ/filigran-copilot"
        "/refs/heads/main/skills/hibob-goals/SKILL.md"
        "?token=ghp_SUPERSECRET1234567890"
    )
    skill = _skill(url_with_token, name="hibob-goals")
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"Client error '404 Not Found' for url '{url_with_token}'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    # The raw token value must not appear anywhere in the captured log output.
    combined = "\n".join(caplog.messages)
    assert "ghp_SUPERSECRET1234567890" not in combined, (
        "token= query-string credential must be redacted from warning log output"
    )
    assert "token=" not in combined, (
        "token= query-string parameter must be stripped before the URL is logged"
    )
    # The skill name, the path (without credentials), and the HTTP status
    # should still be visible for traceability.
    assert "hibob-goals" in combined
    assert "SKILL.md" in combined
    assert "HTTP 404 Not Found" in combined


# ---------------------------------------------------------------------------
# Generic network failure — token must not appear in warning log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_redacts_token_from_url_in_warning(caplog):
    """A generic network error for a URL with ``token=`` must not log the token."""
    url_with_token = (
        "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
        "?token=secret_abc123"
    )
    skill = _skill(url_with_token, name="some-skill")
    db = _FakeDB([skill])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(
        side_effect=httpx.ConnectError(
            f"Failed to connect to host for {url_with_token}"
        )
    )

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    combined = "\n".join(caplog.messages)
    assert "secret_abc123" not in combined, (
        "token value must be redacted from generic exception warning log output"
    )
    assert "some-skill" in combined


# ---------------------------------------------------------------------------
# Successful refresh — token must not appear in debug log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_refresh_redacts_token_from_url_in_debug(caplog):
    """A successful refresh of a URL with ``token=`` must not log the token in debug."""
    url_with_token = (
        "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
        "?token=secret_debug_token"
    )
    skill = _skill(url_with_token, name="debug-skill")
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.text = "---\nname: debug-skill\n---\n# content"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.DEBUG, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    combined = "\n".join(caplog.messages)
    assert "secret_debug_token" not in combined, (
        "token value must be redacted from debug log output on successful refresh"
    )
    assert "token=" not in combined


# ---------------------------------------------------------------------------
# URL without token is still logged (path info preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_failure_logs_path_when_no_token(caplog):
    """For a clean URL without credentials, the path must still appear in logs."""
    url = "https://github.com/openclaw/skills/blob/HEAD/skills/tomstools11/blog-writer/SKILL.md"
    skill = _skill(url, name="Blog Copywriting")
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"Client error '404 Not Found' for url '{url}'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    combined = "\n".join(caplog.messages)
    assert "Blog Copywriting" in combined
    # The path (without query string) should still be visible for traceability.
    assert "SKILL.md" in combined
    assert "HTTP 404 Not Found" in combined


# ---------------------------------------------------------------------------
# Quarantine: consecutive 4xx failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_increments_refresh_failure_count(caplog):
    """A single 404 must increment refresh_failure_count by 1."""
    url = "https://github.com/openclaw/skills/blob/HEAD/skills/tomstools11/blog-writer/SKILL.md"
    skill = _skill(url, name="Blog Copywriting", refresh_failure_count=0)
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"Client error '404 Not Found' for url '{url}'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        caplog.at_level(logging.WARNING, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 1


@pytest.mark.asyncio
async def test_404_at_threshold_logs_quarantine_error(caplog):
    """When refresh_failure_count reaches the threshold an error is logged."""
    url = "https://raw.githubusercontent.com/FiligranHQ/filigran-copilot/refs/heads/main/skills/hibob-goals/SKILL.md"
    # Start at threshold - 1 so this failure tips it over.
    skill = _skill(url, name="hibob-goals", refresh_failure_count=2)
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.reason_phrase = "Not Found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"Client error '404 Not Found' for url '{url}'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
        # Default threshold is 3 — keep it at 3 for this test.
        patch("app.core.config.settings.skill_refresh_quarantine_threshold", 3),
        caplog.at_level(logging.ERROR, logger="app.services.autonomous"),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 3
    # An error-level quarantine message must be present.
    combined = "\n".join(caplog.messages)
    assert "hibob-goals" in combined
    assert "quarantine" in combined.lower() or "auto-refresh disabled" in combined.lower()


@pytest.mark.asyncio
async def test_successful_refresh_resets_failure_count():
    """A successful refresh must reset refresh_failure_count to 0."""
    url = "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
    skill = _skill(url, name="some-skill", refresh_failure_count=2)
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.text = "---\nname: some-skill\n---\n# content"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 0, (
        "refresh_failure_count must reset to 0 after a successful fetch"
    )


@pytest.mark.asyncio
async def test_5xx_does_not_change_failure_count():
    """A 5xx (transient) failure must NOT change refresh_failure_count."""
    url = "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
    skill = _skill(url, name="some-skill", refresh_failure_count=1)
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.reason_phrase = "Service Unavailable"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="Server error '503 Service Unavailable'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 1, (
        "5xx (transient) failures must not increment refresh_failure_count"
    )


@pytest.mark.asyncio
async def test_429_does_not_change_failure_count():
    """HTTP 429 (rate limiting) is transient and must NOT count toward quarantine."""
    url = "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
    skill = _skill(url, name="some-skill", refresh_failure_count=1)
    db = _FakeDB([skill])

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.reason_phrase = "Too Many Requests"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="Client error '429 Too Many Requests'",
        request=MagicMock(),
        response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 1, (
        "429 (rate limiting) must not increment refresh_failure_count"
    )
    # Transient failures get the short ~1 h back-off, not the full 24 h window.
    from datetime import UTC, datetime, timedelta

    assert skill.fetched_at is not None
    assert skill.fetched_at < datetime.now(UTC) - timedelta(hours=22), (
        "429 must use the short transient back-off, not the full refresh window"
    )


@pytest.mark.asyncio
async def test_network_error_does_not_change_failure_count():
    """A network-level timeout must NOT change refresh_failure_count."""
    url = "https://raw.githubusercontent.com/example/repo/main/SKILL.md"
    skill = _skill(url, name="some-skill", refresh_failure_count=1)
    db = _FakeDB([skill])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with (
        patch("app.core.database.release_db_connection_before_loop", new_callable=AsyncMock),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        await _refresh_stale_skills(db)

    assert skill.refresh_failure_count == 1, (
        "Network timeout must not increment refresh_failure_count"
    )
