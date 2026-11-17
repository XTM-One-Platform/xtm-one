# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Self-action echo suppression for GitHub webhook feedback loops.

When an agent writes to GitHub through an integration (edits an issue title,
posts a comment, sets labels, opens a PR, …), GitHub fires a webhook for that
very change.  An assignment subscribed to those events would then re-trigger on
its *own* output and loop forever — exactly the symptom behind the cascade of
title renames a "convention guardian" assignment produced.

This module breaks the loop for assignments that opt in
(``Assignment.ignore_self_triggered_events``).  When a GitHub write tool runs,
:func:`record_self_action` stores a short-lived marker keyed by
``(integration, repo, issue/PR number)``.  The webhook receiver calls
:func:`was_self_action` for the inbound event's target and skips the run when a
live marker exists.

Why target-based rather than ``sender.login``-based?  The agent very commonly
acts through the *owner's own* GitHub credentials (PAT / OAuth), so the inbound
event's author equals the human who would also edit by hand.  Matching on the
*target the agent just wrote to* is therefore the only signal that reliably
distinguishes the agent's echo from a genuine human edit, and it also covers
the GitHub App ``app-slug[bot]`` case without needing to resolve the bot login.

Trade-off: a human edit to the *same* issue within the marker TTL is also
suppressed.  The window is intentionally short and the behaviour is opt-in, so
this is an acceptable price for breaking an otherwise-infinite loop.  Everything
here fails open — a Redis outage never blocks a legitimate webhook.
"""

from __future__ import annotations

import logging
import uuid

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# How long a self-action marker stays live.  Must comfortably exceed the gap
# between the agent's API write and GitHub delivering the resulting webhook
# (usually sub-second), while staying short enough that a genuine human edit
# made a little later still triggers normally.
SELF_ACTION_TTL_SECONDS = 180

_KEY_PREFIX = "github:self_action"


def _canonical_integration_id(integration_id: str) -> str:
    """Normalize an integration id so the recorder and checker share one key.

    The recorder (GitHub write tools) is bound to the id taken from the agent's
    ``integration_bindings``, which may be stored non-canonically (uppercase,
    unhyphenated, ...), while the webhook receiver uses the id from the hook URL
    path (canonical ``str(uuid)``). Folding both to canonical UUID form keeps
    ``record_self_action`` and ``was_self_action`` looking up the *same* key, so
    loop protection can't silently miss because the two sides spelled the id
    differently. A non-UUID value falls back to a stripped, lower-cased form
    (still consistent across both sides).
    """
    try:
        return str(uuid.UUID(str(integration_id)))
    except (ValueError, AttributeError, TypeError):
        return str(integration_id).strip().lower()


def _key(integration_id: str, repo: str, number: int | str) -> str:
    return (
        f"{_KEY_PREFIX}:{_canonical_integration_id(integration_id)}:"
        f"{repo.strip().lower()}#{number}"
    )


async def record_self_action(
    integration_id: str, repo: str, number: int | str
) -> None:
    """Mark that the agent just wrote to ``repo#number`` via ``integration_id``.

    Best-effort: any failure (bad args, Redis down) is swallowed so a tool call
    is never broken by the loop-protection bookkeeping.
    """
    if not integration_id or not repo or number in (None, ""):
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.set(
            _key(integration_id, repo, number),
            "1",
            ex=SELF_ACTION_TTL_SECONDS,
        )
        logger.debug(
            "Recorded GitHub self-action marker integration=%s %s#%s",
            integration_id,
            repo,
            number,
        )
    except Exception as exc:
        logger.debug("Failed to record GitHub self-action marker: %s", format_exception_message(exc))


async def was_self_action(
    integration_id: str, repo: str, number: int | str
) -> bool:
    """Return True when ``repo#number`` has a live self-action marker.

    Fails open (returns False) on any error so loop protection can never block
    a legitimate webhook when Redis is unavailable.
    """
    if not integration_id or not repo or number in (None, ""):
        return False
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        return bool(await redis.get(_key(integration_id, repo, number)))
    except Exception as exc:
        logger.debug("Failed to check GitHub self-action marker: %s", format_exception_message(exc))
        return False


def extract_github_target(payload: dict | None) -> tuple[str, int] | None:
    """Pull ``(repo_full_name, number)`` from a GitHub webhook payload.

    Handles the event families an assignment typically subscribes to:
    ``issues``, ``issue_comment`` (carries an ``issue``), ``pull_request``,
    ``pull_request_review`` and ``pull_request_review_comment`` (carry a
    ``pull_request``).  Returns ``None`` for payloads without an
    issue/PR-scoped target (e.g. ``push``, ``star``), which simply means no
    echo suppression is applied to them.
    """
    if not isinstance(payload, dict):
        return None

    repo = ((payload.get("repository") or {}).get("full_name") or "").strip()
    if not repo:
        return None

    for container_key in ("issue", "pull_request"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            number = container.get("number")
            if isinstance(number, int):
                return repo, number

    # Some PR events surface the number at the top level alongside the object.
    top_number = payload.get("number")
    if isinstance(top_number, int) and isinstance(
        payload.get("pull_request"), dict
    ):
        return repo, top_number

    return None
