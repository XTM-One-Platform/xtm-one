"""Ashby integration - applicant tracking and recruiting.

API: https://developers.ashbyhq.com/reference/introduction (version 2026-01-01).
Auth: HTTP Basic with the API key as the username and no password; each key
holds per-module read / write permissions, plus separate settings for
confidential jobs and private fields. Every endpoint is a POST.

One module per domain, each a ``register(ctx, executor)``:

* ``candidates``    - profiles, search, notes, tags, projects, anonymization.
* ``applications``  - applications (status / job / date filters), stage
                      changes, transfers, stage history, AI evaluations.
* ``feedback``      - interview scorecards, feedback forms, feedback requests.
* ``interviews``    - scheduled interviews and their events, scheduling,
                      interview plans and stages, take-homes, transcripts.
* ``jobs``          - jobs, job postings, openings.
* ``offers``        - offers and the start -> create flow, approvals.
* ``organization``  - users, departments, locations, sources, tags, reference
                      data, custom fields, hiring teams, projects, referrals,
                      reports, surveys, the API key.
* ``files``         - candidate resume and file download / upload.

``_common`` holds the HTTP helper (Ashby answers refusals with HTTP 200 and
``success: false``), paging, per-run lookups, forms and formatters.
"""

from __future__ import annotations

from typing import Any

from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import applications, candidates, feedback, files, interviews, jobs, offers, organization
from ._common import BASE_URL, PROVIDER, AshbyContext, ashby_error, required_permission

__all__ = ["AshbyIntegration", "build_tools", "validate_connection"]

_PROBE = "apiKey.info"


async def _key_info(api_key: str) -> tuple[bool, str, dict[str, Any]]:
    """``(valid, message, key info)`` for *api_key*, from ``apiKey.info``."""
    if not (api_key or "").strip():
        return False, "Missing Ashby API key.", {}
    ctx = AshbyContext(api_key=api_key.strip())
    async with ctx.client() as client:
        resp, err = await safe_request(
            client,
            "post",
            f"{BASE_URL}/{_PROBE}",
            PROVIDER,
            "check the API key",
            expected_statuses={401, 403},
            auth=(ctx.api_key, ""),
            json={},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    if resp is None:
        return False, err or "Ashby did not answer.", {}
    try:
        body = resp.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if resp.status_code == 403 and "missing_endpoint_permission" in str(body):
        return (
            True,
            (
                "Connected to Ashby: the API key is valid. Grant it the "
                f"'{required_permission(_PROBE)}' permission to let get_ashby_api_key_info list "
                "what it may read and write."
            ),
            {},
        )
    if resp.status_code == 401:
        return False, "Ashby received no API key.", {}
    if resp.status_code == 403:
        return False, "Ashby rejected the API key (wrong or deactivated).", {}
    if err:
        return False, err, {}
    refused = ashby_error(body, _PROBE)
    if refused:
        return False, refused, {}
    info = body.get("results") if isinstance(body.get("results"), dict) else {}
    scopes = sorted(str(s) for s in info.get("scopes") or [])
    title = info.get("title") or "untitled"
    return (
        True,
        (
            f"Connected to Ashby with API key '{title}' ({len(scopes)} permissions"
            + (f": {', '.join(scopes)}" if scopes else "")
            + ")."
        ),
        info,
    )


async def validate_connection(api_key: str) -> tuple[bool, str]:
    """``(ok, message)`` for the integration's Test button."""
    ok, message, _ = await _key_info(api_key)
    return ok, message


class AshbyIntegration(BaseIntegration):
    provider_name = "ashby"
    display_name = "Ashby"
    icon = "Users"
    description = (
        "Connect Ashby to search candidates, read applications, interview schedules and "
        "scorecards, track jobs, openings and offers, and move candidates through the pipeline."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        ok, _ = await validate_connection(token)
        return ok

    async def get_user_info(self, token: str) -> dict:
        ok, message, info = await _key_info(token)
        if not ok:
            return {}
        return {"name": info.get("title") or "Ashby API key", "scopes": info.get("scopes") or [], "status": message}


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Ashby tool, for the API key *token*."""
    ctx = AshbyContext(api_key=(token or "").strip(), persist_file_fn=kwargs.get("persist_file_fn"))
    tools: list[ToolDefinition] = []
    for module in (candidates, applications, feedback, interviews, jobs, offers, organization, files):
        tools += module.register(ctx, executor)
    return tools
