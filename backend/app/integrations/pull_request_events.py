# SPDX-License-Identifier: Apache-2.0

"""Pull request events a provider parses for the generic layer.

A work item may name the pull requests it waits on (``payload.pull_requests``,
``services/work_item_pull_requests.py``). What a code host sends when one of
them changes - a CI run starting or ending, the pull request merged, closed or
pushed to - is the host's own vocabulary, so each provider registers a parser
that turns a verified webhook delivery into a :class:`PullRequestEvent`, and
the work-item service never reads a vendor payload. A provider without a
parser is invisible to the feature; a delivery a parser does not recognise
answers ``None`` and costs no database read.

Light on purpose (no model, no service import): provider packages register
their parser when they are imported, like ``lifecycle.register_lifecycle``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

#: ``ci``: a CI run (one workflow, one check suite) of a head commit changed
#: status; ``closed``: the pull request was merged or closed; ``head``: new
#: commits were pushed to it, so the CI of the previous head no longer counts,
#: or it was marked ready for review or converted to a draft (``draft``);
#: ``issue_closed``: an issue was closed (``pr_numbers`` names the issue), which
#: releases the work items blocked on it; ``dequeued``: the code host removed
#: the pull request from its merge queue without merging it (failed queue
#: checks, a conflict in the group, a manual removal), ``conclusion`` naming why.
#: ``tag_created``: a tag was pushed (its name in ``extra["tag"]``); it only hints the
#: ``tag`` references of blocked work items, read back before any release (#2209).
PullRequestEventKind = Literal["ci", "closed", "head", "issue_closed", "dequeued", "tag_created"]


@dataclass(frozen=True)
class PullRequestEvent:
    """One change to a pull request, in the platform's words."""

    kind: PullRequestEventKind
    #: ``owner/name``, lowercased: how a binding names its repository.
    repo: str
    #: The pull requests the change belongs to (a CI run may serve several).
    pr_numbers: tuple[int, ...] = ()
    head_branch: str | None = None
    head_sha: str | None = None
    #: ``ci`` only: one key per CI source of a head commit (a workflow, a
    #: check suite app), so a re-run replaces the run it repeats.
    run_key: str = ""
    #: ``ci`` only: the run's id; a delivery older than the recorded run of the
    #: same key is ignored (ids grow).
    run_id: int | None = None
    #: ``ci`` only: the attempt of the run (a re-run keeps the run id and grows
    #: it), ``None`` when the host does not say. A new attempt is a new
    #: conclusion; a delivery of an older attempt of the same run is ignored.
    run_attempt: int | None = None
    run_name: str | None = None
    #: ``ci`` only: ``queued`` | ``in_progress`` | ``completed``.
    status: str | None = None
    #: ``ci`` only, once completed: ``success``, ``failure``, ``cancelled``...
    conclusion: str | None = None
    run_url: str | None = None
    #: ``ci`` only: a run that does not test the pull request's own head with its
    #: own CI (GitHub: a ``pull_request_target`` run, the base's workflow, or a
    #: third-party app's check suite, which can complete before the head's
    #: workflows are even requested). It can fail a head, never decide its
    #: success alone.
    advisory: bool = False
    #: ``closed`` only.
    merged: bool = False
    merge_sha: str | None = None
    #: ``closed`` and ``issue_closed``: when the reference merged (a merged pull
    #: request) or closed, as the code host states it (ISO 8601); ``None`` when
    #: the host did not say. Never the time the platform saw the event.
    closed_at: str | None = None
    #: ``head`` only: whether the pull request is a draft, ``None`` when the
    #: delivery does not say. A draft is not waiting on its merge yet.
    draft: bool | None = None
    url: str | None = None
    #: Provider-specific facts the generic layer may quote verbatim. ``dequeued``:
    #: ``queue_run_hint``, where the failing merge queue run is read (a removal on
    #: failed checks), quoted in the wake and the surfaced note.
    extra: dict = field(default_factory=dict, hash=False, compare=False)


PullRequestEventParser = Callable[[str, dict], PullRequestEvent | None]

_PARSERS: dict[str, PullRequestEventParser] = {}


def register_pull_request_event_parser(provider: str, parser: PullRequestEventParser) -> None:
    """Register *provider*'s parser. Idempotent (last wins)."""
    _PARSERS[provider] = parser


def parse_pull_request_event(provider: str, event_key: str, payload: object) -> PullRequestEvent | None:
    """The delivery as a :class:`PullRequestEvent`, ``None`` when it is not one.

    Never raises: a payload the parser cannot read is logged and ignored, so
    a malformed delivery never fails the webhook it came with.
    """
    parser = _PARSERS.get(provider)
    if parser is None or not isinstance(payload, dict):
        return None
    try:
        return parser(event_key, payload)
    except Exception as exc:  # a parser bug must not fail a delivery
        logger.warning("Could not parse %s pull request event %s: %s", provider, event_key, exc)
        return None
