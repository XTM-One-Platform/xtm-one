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

"""A blocked work item always says why, and a pull request or issue releases it.

``blocked`` used to be free text: the only native release was ``blocked_on``
(other work items), so an item waiting on a pull request or an issue
stayed blocked after it merged or closed until somebody noticed. Two things
change that:

* **Why** - a ``blocked`` status needs at least one of ``blocked_on`` (work
  items), ``blocked_on_refs`` (code-host references), ``blocked_reason``
  (free text: what it waits for, who releases it, since when) or
  ``needs_human``; :func:`refuse_blocked_without_why` refuses the write
  otherwise, and :func:`blocked_why` is what every view shows of a blocked
  item, ``missing`` for a legacy one that says nothing.
* **Release** - ``blocked_on_refs`` lists references::

      [{"repo": "owner/name", "kind": "pull_request" | "issue", "number": 12,
        "release_on": "merged" | "closed" | "merged_or_closed"}]

  The code-host deliveries the platform already receives
  (:class:`~app.integrations.pull_request_events.PullRequestEvent`: a pull
  request closed, merged or not, and an issue closed) mark the matching
  references satisfied; once every reference of an item is satisfied and it
  waits for no work item, :func:`release_blocked_on_refs` puts it back in
  the queue: ``open``, unclaimed, the dated note ``released: <repo>#<n>
  <merged|closed> at <when it merged or closed>; released at <UTC>`` (the
  code host's time, :func:`release_note`) on top of its description, then
  ``work_item.status_changed`` and ``work_item.released``. The inbox takes it
  after a short grace (:func:`release_not_before`), so the run that merged
  the pull request completes the item instead of racing a second run. A pull request
  closed unmerged that a reference wanted merged (a superseded pull request) can
  never release the item: it is surfaced instead, back to ``open`` and
  unclaimed with the note ``surfaced: <ref> closed unmerged ...`` on top of its
  description (:func:`unmet_note`), so its owner re-points it to the successor
  or completes it, rather than leaving it blocked on a wait that cannot hold.
  ``closed`` means closed without a merge for a pull request;
  ``merged_or_closed`` takes either.

The matcher is pure (:func:`apply_reference_event`); the release is a system
write, like the dependency sweep: no writer, best-effort, never raises. The
note dates the closing as the code host does (``merged_at`` / ``closed_at``,
:func:`closing_stamp`), not the moment the platform learnt of it.

**Unsigned deliveries** - a provider whose OAuth default holds no webhook
secret still delivers, but such a delivery is never trusted with a work
item's state. A closing it announces is read back instead
(:func:`release_from_unverified_delivery`): each reference an item waits for
is read from the provider with its owner's credential, and only a closing the
read confirms is applied, so the release lands within seconds of the merge.

**Polling fallback** - a repository whose webhook is not installed sends no
delivery, and a reference that merged before the item was blocked on it
sends none any more, so :func:`poll_blocked_on_refs` (the scheduler, every
:data:`POLL_EVERY_TICKS` ticks) reads the open references of the items
blocked and quiet for :data:`POLL_MIN_AGE_SECONDS` through the provider's
reader (``integrations/reference_state.py``), with the credential the item's
owner reads that provider with, and applies a closing it finds exactly as
the delivery would have. An item is read again at most once per
:data:`POLL_QUIET_SECONDS` per process; an unreadable reference keeps it
blocked.

**Deployments** - a reference of kind ``deployment``::

      {"repo": "owner/name", "kind": "deployment", "number": 12 | "sha": "1a2b3c4",
       "release_on": "deployed"}

  waits until the change is live: two stages, one item. A pull request not
  merged yet is first matched by the same deliveries, read-backs and polling
  fallback as a ``pull_request`` reference, which record its ``merge_sha``
  (a pull request closed unmerged surfaces the item, as above); then the reference
  waits for a build whose ``git_sha`` contains that commit. The check runs on
  ``platform.release.deployed`` (:func:`on_release_deployed`) and once at the
  write (:func:`schedule_deployment_check`), never on a poll: each provider
  registers a :data:`~app.integrations.reference_state.CommitContainmentReader`
  (GitHub: the compare of the merge commit and the build), answered once per
  ``(repo, merge_sha, build_sha)`` per process.

**Workflow runs** - a reference of kind ``workflow_run``::

      {"repo": "owner/name", "kind": "workflow_run", "workflow": "release.yml",
       "after": "2026-11-06T00:40:00Z", "branch": "main", "release_on": "succeeded"}

  waits for the first successful run of a workflow created at or after
  ``after`` (the time of the write when omitted): a change merged in another
  repository (an infrastructure chart) that reaches production only through
  the next run of a release workflow. An optional ``title_contains`` keeps only
  the runs whose display title holds it (``"refs/tags/"``: the tag build of a
  release workflow that also runs for a push on main, #34). An optional ``carries`` (``<sha>`` or
  ``owner/name@<sha>``, stored as ``carries_sha`` and ``carries_repo``, the reference's repository
  when no repository is named) waits for the release of a merge:
  only a run whose display title names a commit of ``carries_repo`` that is ``carries_sha`` or a
  descendant of it releases the item (a release that runs in another repository, its title
  ``Release <ref> at <sha>``); an older run moves ``after`` past it, so a release of the exact
  commit cancelled by a newer push never strands the item (#1634). The polling fallback reads it through
  the provider's :data:`~app.integrations.reference_state.WorkflowRunReader`
  (:func:`_poll_workflow_run`); the release note names the run. A run that succeeded
  before the item was blocked sends no delivery any more: the write that blocks it
  reads its references once (:func:`schedule_workflow_run_check`), as for a deployment.

**Tags** - a reference of kind ``tag`` (#2209)::

      {"repo": "owner/name", "kind": "tag", "pattern": "*.*.*", "carries": "<sha>",
       "release_on": "created"}

  waits for the first tag of ``repo`` whose name matches the glob ``pattern``
  (``*`` when omitted) and whose commit is ``carries`` or a descendant of it: a
  release cut from a merge, which a ``workflow_run`` on a workflow that also runs
  on the main push cannot tell from that push. Read through the provider's
  :data:`~app.integrations.reference_state.TagReader` by the polling fallback
  (:func:`_poll_tag`), at the write that blocks it (:func:`schedule_tag_check`)
  and on a tag creation delivery (:func:`schedule_tag_release`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, select

from app.core.utils import format_exception_message
from app.integrations.pull_request_events import PullRequestEvent
from app.models.work_item import WorkItem

logger = logging.getLogger(__name__)

REF_KINDS = ("pull_request", "issue", "deployment", "workflow_run", "tag", "a2a_task")
#: How an ``a2a_task`` reference is released (#2265): the task completed, or ended whatever its outcome.
A2A_RELEASE_ON = ("completed", "terminal")
RELEASE_ON = ("merged", "closed", "merged_or_closed", "deployed", "succeeded", "created", *A2A_RELEASE_ON)
#: The longest task id an ``a2a_task`` reference may name.
MAX_A2A_TASK_CHARS = 200
#: The states the platform records on an ``a2a_task`` reference whose task ended.
A2A_ENDED_STATES = ("completed", "ended")
#: A tag name glob a ``tag`` reference may give (#2209).
_TAG_PATTERN_RE = re.compile(r"^[A-Za-z0-9._*?/-]{1,200}$")
#: What the tag stage remembers of a reference (with ``state`` and ``at``).
_TAG_KEPT = ("tag", "tag_commit")
#: A workflow a ``workflow_run`` reference may name: its file name or its id.
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
#: The longest ``title_contains`` a ``workflow_run`` reference may give.
MAX_TITLE_CONTAINS_CHARS = 200
#: What the workflow run stage remembers of a reference (with ``state`` and ``at``).
_WORKFLOW_RUN_KEPT = ("run_id", "run_url", "run_at", "run_commit")
#: A commit named in a release run's display title (``Release refs/heads/main at <sha>``).
_TITLE_SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
#: Older runs of a ``carries_sha`` reference skipped in one read, at most (#1634); the
#: position reached is kept as ``carry_after`` (outside the reference key), so the next
#: read resumes past the runs already skipped.
MAX_CARRY_SKIPS = 5
#: A commit a deployment reference may name.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
#: What the deploy stage remembers of a reference (with ``state`` and ``at``);
#: ``carried_by`` names the work item whose pull request the platform carried
#: the reference from (:func:`carried_deployment_ref`).
_DEPLOYMENT_KEPT = ("merge_sha", "merged_at", "build", "carried_by")
#: References one item may name.
MAX_REFS = 20
#: Free-text reason kept per item.
MAX_REASON_CHARS = 2000
#: Items one delivery may release.
_MAX_ITEMS_PER_EVENT = 50
#: Blocked items naming a repository one delivery, or one polling pass, scans for
#: the ones it concerns: the cap above applies after the match, never before it, so
#: a long tail of older items on the same repository cannot hide the item a
#: closing names (an item re-parked after a merge queue removal stayed blocked
#: about 20 h behind 50 older ones).
_CANDIDATE_SCAN = 2000
#: Polling fallback: an item blocked and quiet this long has its references read
#: (the race of a reference merged before the item was blocked on it included).
POLL_MIN_AGE_SECONDS = 2 * 60
#: Polling fallback: an item's references are read again after this long.
POLL_QUIET_SECONDS = 30 * 60
#: Polling fallback: scheduler ticks between two passes (60 s ticks).
POLL_EVERY_TICKS = 5
#: Polling fallback: items, and provider reads, one pass may make.
POLL_MAX_ITEMS = 25
POLL_MAX_READS = 50
#: A release by a pull request or issue closing waits this long before the inbox
#: may take the item, so the run that merged it completes it instead of racing a
#: second run dispatched on the same delivery (:func:`release_not_before`).
RELEASE_GRACE_SECONDS = 45

WHY_FIELDS_MESSAGE = (
    "a blocked work item is black or white: it needs owner_agent_id (the agent that resumes it) and a "
    "blocker the platform releases by itself: blocked_on (the work items it waits for), "
    "blocked_on_refs (the pull requests or issues it waits for: "
    '[{"repo": "owner/name", "kind": "pull_request" | "issue", "number": N, '
    '"release_on": "merged" | "closed" | "merged_or_closed"}], or the deployment of a change: '
    '{"repo": "owner/name", "kind": "deployment", "number": N | "sha": "1a2b3c4", '
    '"release_on": "deployed"}, or the first successful run of a workflow after a time: '
    '{"repo": "owner/name", "kind": "workflow_run", "workflow": "release.yml", '
    '"after": "2026-11-06T00:40:00Z", "branch": "main"}, or the first tag carrying a commit: '
    '{"repo": "owner/name", "kind": "tag", "pattern": "*.*.*", "carries": "1a2b3c4"}), '
    "or a person's decision: needs_human: true with an ask {question, options: [{key, label}], "
    "recommendation: an option key}. blocked_reason is context, never the blocker; a deferral is an "
    "open item with not_before"
)

ASK_REQUIRED_MESSAGE = (
    "needs_human needs an ask card, written in the same call: ask {question, context (2 to 4 lines), "
    "options: [{key, label, effect?}] (at least 2), recommendation: the key of one option}; the person "
    "decides from it and the answer reopens the item"
)

#: The fewest options an ask card offers: a decision is a choice.
MIN_ASK_OPTIONS = 2


class BlockedRefError(ValueError):
    """A ``blocked_on_refs`` entry the platform cannot match."""


# -- The references --------------------------------------------------------------


def _normalize_ref(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise BlockedRefError("each blocked_on_refs entry must be an object {repo, kind, number, release_on}")
    if entry.get("kind") == "a2a_task":
        # Not a code-host reference: no repository (#2265).
        return _normalize_a2a_task_ref(entry)
    repo = entry.get("repo")
    if not isinstance(repo, str) or repo.strip().count("/") != 1 or " " in repo.strip():
        raise BlockedRefError(f"blocked_on_refs: repo must read owner/name, got {repo!r}")
    kind = entry.get("kind") or "pull_request"
    if kind not in REF_KINDS:
        raise BlockedRefError(f"blocked_on_refs: kind must be one of {', '.join(REF_KINDS)}, got {kind!r}")
    if kind == "deployment":
        return _normalize_deployment_ref(entry, repo.strip().lower())
    if kind == "workflow_run":
        return _normalize_workflow_run_ref(entry, repo.strip().lower())
    if kind == "tag":
        return _normalize_tag_ref(entry, repo.strip().lower())
    number = _number(entry.get("number"))
    default = "merged_or_closed" if kind == "pull_request" else "closed"
    release_on = entry.get("release_on") or default
    if release_on not in RELEASE_ON:
        raise BlockedRefError(
            f"blocked_on_refs: release_on must be one of {', '.join(RELEASE_ON)}, got {release_on!r}"
        )
    if release_on == "deployed":
        raise BlockedRefError('blocked_on_refs: release_on deployed needs kind "deployment"')
    if release_on == "succeeded":
        raise BlockedRefError('blocked_on_refs: release_on succeeded needs kind "workflow_run"')
    if release_on in A2A_RELEASE_ON:
        raise BlockedRefError(f'blocked_on_refs: release_on {release_on} needs kind "a2a_task"')
    if release_on == "created":
        raise BlockedRefError('blocked_on_refs: release_on created needs kind "tag"')
    if kind == "issue" and release_on == "merged":
        raise BlockedRefError("blocked_on_refs: an issue is never merged; use release_on closed")
    out: dict[str, Any] = {
        "repo": repo.strip().lower(),
        "kind": kind,
        "number": number,
        "release_on": release_on,
    }
    # ``branch`` is the tool's word for it (the field a workflow_run already takes).
    branch = entry.get("head_branch") or (entry.get("branch") if kind == "pull_request" else None)
    if branch is not None and branch != "":
        if kind != "pull_request":
            raise BlockedRefError("blocked_on_refs: head_branch is followed on a pull_request reference only")
        if not isinstance(branch, str) or not _BRANCH_RE.match(branch.strip()):
            raise BlockedRefError(f"blocked_on_refs: head_branch must be a branch name, got {branch!r}")
        out["head_branch"] = branch.strip()
        # The pull requests of that branch seen closed unmerged, kept on a rewrite.
        superseded = entry.get("superseded")
        if isinstance(superseded, list):
            kept = sorted({n for n in superseded if isinstance(n, int) and not isinstance(n, bool) and n > 0})
            if kept:
                out["superseded"] = kept
        if isinstance(entry.get("merged_number"), int) and not isinstance(entry.get("merged_number"), bool):
            out["merged_number"] = entry["merged_number"]
    # The satisfied state the platform recorded, kept on a rewrite of the same reference.
    for key in ("state", "at"):
        if isinstance(entry.get(key), str):
            out[key] = entry[key]
    return out


def _number(number: object) -> int:
    if isinstance(number, str) and number.strip().lstrip("#").isdigit():
        number = int(number.strip().lstrip("#"))
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise BlockedRefError(f"blocked_on_refs: number must be a positive integer, got {number!r}")
    return number


def _normalize_deployment_ref(entry: dict, repo: str) -> dict:
    release_on = entry.get("release_on") or "deployed"
    if release_on != "deployed":
        raise BlockedRefError(f"blocked_on_refs: a deployment is released on deployed only, got {release_on!r}")
    out: dict[str, Any] = {"repo": repo, "kind": "deployment", "release_on": "deployed"}
    sha = entry.get("sha")
    if entry.get("number") is not None:
        out["number"] = _number(entry.get("number"))
    elif isinstance(sha, str) and _SHA_RE.match(sha.strip().lower()):
        out["sha"] = sha.strip().lower()
        out["merge_sha"] = out["sha"]
    else:
        raise BlockedRefError(
            "blocked_on_refs: a deployment names its pull request (number) or its commit "
            f"(sha, 7 to 40 hexadecimal characters), got number={entry.get('number')!r} sha={sha!r}"
        )
    for key in ("state", "at", *_DEPLOYMENT_KEPT):
        if key not in out and isinstance(entry.get(key), str):
            out[key] = entry[key]
    return out


def _normalize_a2a_task_ref(entry: dict[str, Any]) -> dict[str, Any]:
    """An ``a2a_task`` reference: ``{kind, task, release_on}`` (#2265).

    *task* is the ``collect_task_id`` an outbound delegation answered (the partner's
    task id); whether the item's owner has such a delegation is checked at the write
    (``work_item_a2a_refs.refuse_unknown_a2a_tasks``), never here. Pure.
    """
    task = entry.get("task")
    text = task.strip() if isinstance(task, str) else ""
    if not text or len(text) > MAX_A2A_TASK_CHARS or any(c.isspace() for c in text):
        raise BlockedRefError(
            f"blocked_on_refs: an a2a_task names its task (the collect_task_id the delegation answered), got {task!r}"
        )
    release_on = entry.get("release_on") or "terminal"
    if release_on not in A2A_RELEASE_ON:
        raise BlockedRefError(
            f"blocked_on_refs: an a2a_task is released on {' or '.join(A2A_RELEASE_ON)}, got {release_on!r}"
        )
    out: dict[str, Any] = {"kind": "a2a_task", "task": text, "release_on": release_on}
    # The end the platform recorded, kept on a rewrite of the same reference.
    for key in ("state", "at", "outcome"):
        if isinstance(entry.get(key), str):
            out[key] = entry[key]
    return out


def _normalize_workflow_run_ref(entry: dict, repo: str) -> dict:
    release_on = entry.get("release_on") or "succeeded"
    if release_on != "succeeded":
        raise BlockedRefError(f"blocked_on_refs: a workflow_run is released on succeeded only, got {release_on!r}")
    workflow = entry.get("workflow")
    if isinstance(workflow, int) and not isinstance(workflow, bool):
        workflow = str(workflow)
    if not isinstance(workflow, str) or not _WORKFLOW_RE.match(workflow.strip()):
        raise BlockedRefError(
            f"blocked_on_refs: a workflow_run names its workflow (file name such as release.yml, or id), got {workflow!r}"
        )
    after_raw = entry.get("after")
    if after_raw is None or after_raw == "":
        # Omitted: the first successful run from the write on.
        after = _stamp(datetime.now(UTC))
    else:
        parsed = _host_stamp(after_raw) if isinstance(after_raw, str) else None
        if parsed is None:
            raise BlockedRefError(f"blocked_on_refs: after must be an ISO 8601 time, got {after_raw!r}")
        after = parsed
    out: dict[str, Any] = {
        "repo": repo,
        "kind": "workflow_run",
        "workflow": workflow.strip(),
        "after": after,
        "release_on": "succeeded",
    }
    branch = entry.get("branch")
    if branch is not None and branch != "":
        if not isinstance(branch, str) or not _BRANCH_RE.match(branch.strip()):
            raise BlockedRefError(f"blocked_on_refs: branch must be a branch name, got {branch!r}")
        out["branch"] = branch.strip()
    title = entry.get("title_contains")
    if title is not None and title != "":
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CONTAINS_CHARS:
            raise BlockedRefError(
                "blocked_on_refs: title_contains must be a non-blank text of at most "
                f"{MAX_TITLE_CONTAINS_CHARS} characters, got {title!r}"
            )
        out["title_contains"] = title
    out.update(_carries(entry, repo))
    for key in ("state", "at", "carry_after", *_WORKFLOW_RUN_KEPT):
        if isinstance(entry.get(key), str | int) and not isinstance(entry.get(key), bool):
            out[key] = entry[key]
    return out


def _normalize_tag_ref(entry: dict[str, Any], repo: str) -> dict[str, Any]:
    """A ``tag`` reference: the first tag matching ``pattern`` that carries ``carries`` (#2209)."""
    release_on = entry.get("release_on") or "created"
    if release_on != "created":
        raise BlockedRefError(f"blocked_on_refs: a tag is released on created only, got {release_on!r}")
    pattern = entry.get("pattern")
    if pattern is None or pattern == "":
        pattern = "*"
    if not isinstance(pattern, str) or not _TAG_PATTERN_RE.match(pattern.strip()):
        raise BlockedRefError(f"blocked_on_refs: a tag pattern is a tag name glob such as *.*.*, got {pattern!r}")
    carried = _carries(entry, repo)
    if not carried.get("carries_sha"):
        raise BlockedRefError(
            "blocked_on_refs: a tag reference names the commit the tag must carry (carries, 7 to 40 "
            "hexadecimal characters)"
        )
    if carried.get("carries_repo"):
        raise BlockedRefError("blocked_on_refs: a tag carries a commit of its own repository")
    out: dict[str, Any] = {
        "repo": repo,
        "kind": "tag",
        "pattern": pattern.strip(),
        "carries_sha": carried["carries_sha"],
        "release_on": "created",
    }
    for key in ("state", "at", *_TAG_KEPT):
        if isinstance(entry.get(key), str):
            out[key] = entry[key]
    return out


def _carries(entry: dict[str, Any], repo: str) -> dict[str, str]:
    """``carries_sha`` and ``carries_repo`` of a workflow_run reference's ``carries`` (#1634).

    ``carries`` reads ``<sha>`` or ``owner/name@<sha>``; the stored fields are kept on a rewrite.
    """
    raw = entry.get("carries")
    if raw is None or raw == "":
        sha, carries_repo = entry.get("carries_sha"), entry.get("carries_repo")
        if sha is None or sha == "":
            return {}
        raw = f"{carries_repo}@{sha}" if isinstance(carries_repo, str) and carries_repo else sha
    if not isinstance(raw, str):
        raise BlockedRefError(f"blocked_on_refs: carries must be a commit, or owner/name@ then a commit, got {raw!r}")
    named, _, sha = raw.strip().lower().rpartition("@")
    if not _SHA_RE.match(sha) or (named and (named.count("/") != 1 or " " in named)):
        raise BlockedRefError(
            "blocked_on_refs: carries must be a commit, or owner/name@ then a commit (7 to 40 hexadecimal characters), "
            f"got {raw!r}"
        )
    out = {"carries_sha": sha}
    if named and named != repo:
        out["carries_repo"] = named
    return out


def _ref_key(ref: dict) -> tuple:
    if ref.get("kind") == "workflow_run":
        return (
            ref.get("repo"),
            "workflow_run",
            ref.get("workflow"),
            ref.get("branch"),
            ref.get("after"),
            ref.get("title_contains"),
            ref.get("carries_sha"),
        )
    if ref.get("kind") == "a2a_task":
        return (None, "a2a_task", ref.get("task"))
    if ref.get("kind") == "tag":
        return (ref.get("repo"), "tag", ref.get("pattern"), ref.get("carries_sha"))
    return (ref.get("repo"), ref.get("kind"), ref.get("number") or ref.get("sha"))


def normalize_blocked_on_refs(entries: object) -> list[dict]:
    """*entries* as references, deduplicated in order. Raises :class:`BlockedRefError`."""
    if entries is None:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list | tuple):
        raise BlockedRefError("blocked_on_refs must be a list of {repo, kind, number, release_on}")
    out: list[dict] = []
    seen: set[tuple] = set()
    for entry in entries:
        ref = _normalize_ref(entry)
        key = _ref_key(ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    if len(out) > MAX_REFS:
        raise BlockedRefError(f"blocked_on_refs names at most {MAX_REFS} references")
    return out


def observed_deployment_repositories() -> frozenset[str]:
    """The repositories whose deployments the platform observes (``owner/name``, lower case)."""
    from app.core.config import settings

    raw = str(getattr(settings, "work_item_deployment_repositories", "") or "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def unobserved_deployment_message(repo: str) -> str:
    """Why a deployment reference on *repo* is refused, with the alternatives. Pure."""
    return (
        f"blocked_on_refs: the platform does not observe the deployments of {repo}, so a "
        f"deployment reference on it would never release the item. Wait instead on a "
        f'workflow_run reference ({{"repo": "{repo}", "kind": "workflow_run", '
        f'"workflow": "release.yml", "branch": "main", "carries": "{repo}@" followed by the merge commit}}, '
        f"with the repository and file name of its release workflow; carries releases it only on a run "
        f"whose title names that commit or a later one), or set blocked_reason "
        f"(what to check, who releases it) with a dated not_before for the re-check"
    )


def refuse_unobserved_deployment_refs(refs: list[dict] | None) -> None:
    """Raise :class:`BlockedRefError` on a deployment reference the platform cannot release.

    A deployment reference is released when a running build carries its merge
    (:func:`on_release_deployed`): only the repositories the platform's builds
    come from (``work_item_deployment_repositories``) can ever satisfy it. One
    already satisfied (``state`` deployed) is history and kept as is.
    """
    observed = observed_deployment_repositories()
    for ref in refs or []:
        if ref.get("kind") != "deployment" or _satisfied(ref):
            continue
        if str(ref.get("repo") or "").lower() not in observed:
            raise BlockedRefError(unobserved_deployment_message(str(ref.get("repo"))))


def carried_deployment_ref(repo: str, number: int, carried_by: str) -> dict | None:
    """The deployment reference an item blocked on work item *carried_by* gets
    when that item names pull request *repo*#*number*, ``None`` when the platform
    does not observe the deployments of *repo* (the dependency then releases it
    at the completion, as before). Pure but for the settings read.

    A work item completes at its pull request's merge, before any build carries
    it: an item waiting for the fix waits for its deployment instead (#9943).
    """
    repo = (repo or "").strip().lower()
    if repo not in observed_deployment_repositories():
        return None
    return {
        "repo": repo,
        "kind": "deployment",
        "number": _number(number),
        "release_on": "deployed",
        "carried_by": str(carried_by),
    }


def carried_note(ref: dict, now: datetime) -> str:
    """The note of an item whose wait was carried to *ref*. Pure."""
    return (
        f"carried: waits for the {ref_label(ref)}, the pull request of work item {ref.get('carried_by')}; "
        f"released by the first build that carries its merge, not by that item's completion; "
        f"carried at {_stamp(now)}"
    )


def normalize_blocked_reason(reason: object) -> str | None:
    """The trimmed reason, ``None`` when empty."""
    if reason is None:
        return None
    text = " ".join(str(reason).split())
    return text[:MAX_REASON_CHARS] or None


def ref_label(ref: dict) -> str:
    """``owner/name#12`` (a pull request) or ``owner/name issue #12``."""
    if ref.get("kind") == "issue":
        return f"{ref['repo']} issue #{ref['number']}"
    if ref.get("kind") == "a2a_task":
        return f"A2A task {ref.get('task')}"
    if ref.get("kind") == "workflow_run":
        on = f" on {ref['branch']}" if ref.get("branch") else ""
        titled = f" titled '*{ref['title_contains']}*'" if ref.get("title_contains") else ""
        carrying = ""
        if ref.get("carries_sha"):
            carrying = f" carrying {ref.get('carries_repo') or ref['repo']}@{str(ref['carries_sha'])[:7]}"
        return f"a successful {ref['repo']} {ref.get('workflow')} run{on}{titled}{carrying} after {ref.get('after')}"
    if ref.get("kind") == "tag":
        return f"a {ref['repo']} tag matching {ref.get('pattern')} carrying {str(ref.get('carries_sha') or '')[:7]}"
    if ref.get("kind") == "deployment":
        if ref.get("number"):
            return f"deployment of {ref['repo']}#{ref['number']}"
        return f"deployment of {ref['repo']}@{str(ref.get('sha') or '')[:7]}"
    if ref.get("head_branch"):
        return f"{ref['repo']}#{ref['number']} (or its successor on {ref['head_branch']})"
    return f"{ref['repo']}#{ref['number']}"


def _satisfied(ref: dict) -> bool:
    if ref.get("kind") == "deployment":
        return ref.get("state") == "deployed"
    if ref.get("kind") == "workflow_run":
        return ref.get("state") == "succeeded"
    if ref.get("kind") == "a2a_task":
        return ref.get("state") in A2A_ENDED_STATES
    if ref.get("kind") == "tag":
        return ref.get("state") == "created"
    return ref.get("state") in ("merged", "closed")


def _running_version() -> str:
    from app.services.platform_builds import running_build

    return running_build()[0]


def waiting_label(ref: dict) -> str:
    """What the Waits for line says of one open reference. Pure but for the running version."""
    if ref.get("kind") in ("workflow_run", "tag"):
        return ref_label(ref)
    if ref.get("kind") != "deployment":
        return f"{ref_label(ref)} ({ref.get('release_on')})"
    merge_sha = str(ref.get("merge_sha") or "")
    if not merge_sha:
        return f"{ref_label(ref)} (not merged yet)"
    merged = f"merged {merge_sha[:7]}, " if ref.get("number") else ""
    if ref.get("build"):
        return f"{ref_label(ref)} ({merged}not in build {ref['build']})"
    # Never read against a build (its commit was unknown or unreadable): say so.
    return f"{ref_label(ref)} ({merged}not checked yet against build {_running_version()})"


def has_why(
    *,
    blocked_on: object = None,
    blocked_on_refs: object = None,
    blocked_reason: object = None,
    needs_human: object = None,
) -> bool:
    """Whether a blocked item with these fields says what it waits for."""
    return bool(blocked_on) or bool(blocked_on_refs) or bool(normalize_blocked_reason(blocked_reason)) or bool(
        needs_human
    )


def ask_is_complete(ask: object) -> bool:
    """Whether *ask* is a decision card a person can answer: a question, options, a recommendation. Pure."""
    if not isinstance(ask, dict):
        return False
    options = ask.get("options")
    return (
        bool(str(ask.get("question") or "").strip())
        and isinstance(options, list)
        and len(options) >= MIN_ASK_OPTIONS
        and bool(str(ask.get("recommendation") or "").strip())
    )


def bound_open_pull_request(payload: object) -> bool:
    """Whether *payload* binds an open pull request, whose CI completion or closing releases the item. Pure.

    The wait ``work_item_pull_requests.parked_without_release`` hands back: a
    draft between two slices, parked by the inbox finalizer (#10692).
    """
    from app.services.work_item_pull_requests import pull_request_bindings

    return any(b.get("state", "open") == "open" for b in pull_request_bindings(payload))


def blocked_defects(
    *,
    owner_agent_id: object = None,
    blocked_on: object = None,
    blocked_on_refs: object = None,
    needs_human: object = None,
    ask: object = None,
    bound_open_pull_request: bool = False,
) -> list[str]:
    """What keeps a blocked item with these fields from being black or white, ``[]`` when nothing. Pure.

    Blocked is either a real blocker the platform releases by itself
    (``blocked_on``, ``blocked_on_refs``, or an open pull request bound in the
    payload, released by its CI completion or its closing:
    *bound_open_pull_request*) or a person's decision (``needs_human`` with a
    complete :func:`ask_is_complete` card), and always an owner that resumes
    it. ``blocked_reason`` and ``not_before`` are never a blocker.
    """
    out: list[str] = []
    if not owner_agent_id:
        out.append("no owner_agent_id")
    if needs_human and not ask_is_complete(ask):
        out.append("needs_human without an ask card")
    elif not (blocked_on or blocked_on_refs or needs_human or bound_open_pull_request):
        out.append("no blocker the platform releases (free text or nothing)")
    return out


def refuse_blocked_without_why(status: str | None, **fields: object) -> None:
    """Raise ``ValueError`` naming the three options when *status* is ``blocked`` and not black or white.

    *fields* are those of :func:`blocked_defects`; ``blocked_reason`` is
    accepted and ignored (context, never the blocker); ``payload`` (the item's
    payload as it lands) counts an open bound pull request as a blocker.
    """
    if status != "blocked":
        return
    fields.pop("blocked_reason", None)
    payload = fields.pop("payload", None)
    if payload is not None and "bound_open_pull_request" not in fields:
        fields["bound_open_pull_request"] = bound_open_pull_request(payload)
    defects = blocked_defects(**fields)
    if defects:
        raise ValueError(f"{WHY_FIELDS_MESSAGE} (missing: {'; '.join(defects)}); nothing was written")


def refuse_needs_human_without_ask(needs_human: object, ask: object) -> None:
    """Raise ``ValueError`` when an item waits for a person with no decision card to answer."""
    if needs_human and not ask_is_complete(ask):
        raise ValueError(f"{ASK_REQUIRED_MESSAGE}; nothing was written")


#: An item blocked on its own red pull request is released after this long.
RED_SELF_BLOCK_SECONDS = 30 * 60


def _own_red_refs(refs: object, payload: object) -> list[dict]:
    """The references of *refs* that wait on the merge of a pull request *payload* names red. Pure.

    A ``pull_request`` reference, or a ``deployment`` one not merged yet, naming
    an open pull request the item is bound to whose CI on the current head
    failed (``work_item_pull_requests.red_pull_requests``).
    """
    from app.services.work_item_pull_requests import red_pull_requests

    red = {(b["repo"], b["number"]): b for b in red_pull_requests(payload)}
    if not red:
        return []
    out: list[dict] = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or _satisfied(ref):
            continue
        if ref.get("kind") == "deployment" and ref.get("merge_sha"):
            continue  # merged already: it waits for a build, not for the red head
        if ref.get("kind") not in ("pull_request", "deployment"):
            continue
        binding = red.get((str(ref.get("repo") or "").lower(), ref.get("number")))
        if binding is not None:
            out.append({**ref, "failed_at": binding.get("failed_at")})
    return out


def refuse_blocked_on_red_pull_request(
    status: str | None, *, blocked_on_refs: object, payload: object, blocked_on: object = None
) -> None:
    """Raise ``ValueError`` when a ``blocked`` item would wait on the merge of its own red pull request.

    A red head merges only once its owner pushes a fix, and that owner is the
    item: parked on the merge, nothing would ever wake it. Allowed when the item
    also waits for work items (``blocked_on``): those are its waker.
    """
    if status != "blocked" or blocked_on:
        return
    stuck = _own_red_refs(blocked_on_refs, payload)
    if stuck:
        names = ", ".join(ref_label(r) for r in stuck)
        raise ValueError(
            f"{names} is red on its head: the item would wait forever on its merge. Fix the head and push "
            "(the CI completion dispatches the item again), or block on the work item that fixes it (blocked_on)"
        )


#: A pull request named in a reason: ``owner/name#12``, a hyphenated repository name
#: (``filigran-ui #516``), ``PR 12`` or ``pull request #12``.
_REASON_REFERENCE_RE = re.compile(
    r"(?:\b[\w.-]+/[\w.-]+\s?#\d+)"
    r"|(?:\b(?:PR|pull[ -]request)\s?#?\d+)"
    r"|(?:\b[a-z0-9]+(?:[-.][a-z0-9]+)+\s?#\d+)",
    re.IGNORECASE,
)


def reason_names_reference(reason: object) -> str | None:
    """The first pull request or issue *reason* names, as written; ``None`` when it names none. Pure."""
    if not isinstance(reason, str) or not reason:
        return None
    match = _REASON_REFERENCE_RE.search(reason)
    return match.group(0) if match else None


def refuse_unbound_reference_reason(
    status: str | None,
    *,
    blocked_reason: object,
    blocked_on: object = None,
    blocked_on_refs: object = None,
    needs_human: object = None,
    payload: object = None,
) -> None:
    """Raise ``ValueError`` when a ``blocked`` item waits on a pull request it only names in its reason.

    A reason is text: no event reads it. With no work item to wait for, no open
    reference, no person to answer and no open pull request bound to the item,
    nothing would ever release it (#11045). The fix is a reference the platform
    watches (``blocked_on_refs``), or the item that carries the change (``blocked_on``).
    """
    from app.services.work_item_pull_requests import pull_request_bindings

    if status != "blocked" or blocked_on or needs_human or open_refs(blocked_on_refs):
        return
    if any(b.get("state", "open") == "open" for b in pull_request_bindings(payload)):
        return  # parked on a bound pull request: its CI or its closing releases it
    named = reason_names_reference(blocked_reason)
    if named is None:
        return
    raise ValueError(
        f"the reason names '{named}', which nothing watches: the item would stay blocked after it merges or "
        'closes. Give blocked_on_refs, e.g. [{"repo": "owner/name", "kind": "pull_request", "number": 12, '
        '"release_on": "merged_or_closed"}] or {"kind": "deployment", "release_on": "deployed"} for a '
        "build, or blocked_on (the work item that carries it), or needs_human: true"
    )


#: The keys of the items that are about one pull request: a UX verdict to post on it
#: (``dev:ux-verdict-12-comment``) or a UX review of it (``ux:xtm-one-12``, ``ux:gate:xtm-one-12``).
_OWN_PULL_REQUEST_KEY_RES = (
    re.compile(r"^dev:ux-verdict-(?P<number>\d+)-comment$"),
    re.compile(r"^ux:(?:gate:)?(?P<name>[a-z0-9][a-z0-9._-]*?)-(?P<number>\d+)$"),
)
#: ``owner/name#12`` as an item title names its pull request.
_TITLE_PULL_REQUEST_RE = re.compile(r"\b(?P<repo>[\w.-]+/[\w.-]+)\s?#(?P<number>\d+)\b")


def own_pull_request(correlation_key: object, title: object) -> dict | None:
    """``{repo, number}`` of the pull request an item is about, ``None`` when it is about none. Pure.

    Read from its key (the number) and its title (``owner/name#12``, the repository);
    *repo* is ``None`` when neither names it, *name* the repository's short name the
    key gives (``ux:xtm-one-12``). A title naming another number is ignored.
    """
    if not isinstance(correlation_key, str):
        return None
    key = correlation_key.strip().lower()
    for pattern in _OWN_PULL_REQUEST_KEY_RES:
        match = pattern.match(key)
        if match is None:
            continue
        number = int(match.group("number"))
        name = match.groupdict().get("name")
        repo = None
        if isinstance(title, str):
            for named in _TITLE_PULL_REQUEST_RE.finditer(title):
                candidate = named.group("repo").lower()
                if int(named.group("number")) == number and (name is None or candidate.split("/")[1] == name):
                    repo = candidate
                    break
        return {"repo": repo, "name": name, "number": number}
    return None


def _bound_pull_requests(payload: object) -> set[tuple[str, int]]:
    """``(repo, number)`` of every pull request *payload* binds (``payload.pull_requests``). Pure."""
    from app.services.work_item_pull_requests import pull_request_bindings

    return {(str(b.get("repo") or "").lower(), int(b["number"])) for b in pull_request_bindings(payload)}


def _foreign_pull_request_refs(refs: object, own: dict[str, Any], payload: object = None) -> list[dict[str, Any]]:
    """The open references of *refs* naming another pull request of *own*'s repository. Pure.

    A pull request the item's own payload binds is never foreign: the payload is
    where the item says which pull request carries its change (#2242).
    """
    bound = _bound_pull_requests(payload)
    out: list[dict[str, Any]] = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or _satisfied(ref):
            continue
        if ref.get("kind") not in ("pull_request", "deployment") or not ref.get("number"):
            continue
        repo = str(ref.get("repo") or "").lower()
        same_repo = (
            repo == own["repo"]
            if own.get("repo")
            else (own.get("name") is not None and repo.split("/")[-1] == own["name"])
        )
        if same_repo and ref["number"] != own["number"] and (repo, int(ref["number"])) not in bound:
            out.append(ref)
    return out


def refuse_foreign_pull_request_ref(
    status: str | None,
    *,
    correlation_key: object,
    title: object,
    blocked_on_refs: object,
    payload: object = None,
) -> None:
    """Raise ``ValueError`` when an item about pull request N waits on another pull request of its repository.

    A UX verdict or review item belongs to one pull request: parked on a sibling's
    (a stale number, a superseded or neighbouring PR of the same program), it wakes
    on that sibling's event or never (gap family verdict-item-pr-ref, #11507).
    The refusal names the reference expected.
    """
    if status != "blocked":
        return
    own = own_pull_request(correlation_key, title)
    if own is None:
        return
    foreign = _foreign_pull_request_refs(blocked_on_refs, own, payload)
    if not foreign:
        return
    repo = own.get("repo") or str(foreign[0].get("repo") or "").lower()
    names = ", ".join(ref_label(r) for r in foreign)
    expected = f"{repo}#{own['number']}"
    raise ValueError(
        f"{names} is not this item's pull request: {correlation_key} is about {expected}. Block on "
        f'{{"repo": "{repo}", "kind": "pull_request", "number": {own["number"]}, "release_on": '
        '"merged_or_closed"}, or on the work item that carries the other change (blocked_on)'
    )


def red_self_block_note(stuck: list[dict], now: datetime) -> str:
    """The note of an item released from a wait on its own red pull request."""
    names = ", ".join(ref_label(r) for r in stuck)
    return (
        f"released: {names} red on its head for more than {RED_SELF_BLOCK_SECONDS // 60} minutes, so it cannot "
        f"merge and the item would wait forever; back in the queue to fix the head ({_stamp(now)})"
    )


def red_self_block_due(stuck: list[dict], last_activity_at: datetime | None, now: datetime) -> bool:
    """Whether every reference of *stuck* has been red for :data:`RED_SELF_BLOCK_SECONDS`. Pure.

    A failure recorded before ``failed_at`` existed counts from the item's last activity.
    """
    if not stuck:
        return False
    limit = now - timedelta(seconds=RED_SELF_BLOCK_SECONDS)
    for ref in stuck:
        since = _parse_stamp(ref.get("failed_at")) or last_activity_at
        if since is None or since > limit:
            return False
    return True


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def blocked_why(item: Any) -> dict | None:
    """What the views show of a blocked item: ``{"text", "missing"}``; ``None`` when not blocked.

    ``text`` reads ``Waits for: ...`` (the items, the references, the reason,
    or a person); ``missing`` is true for a legacy blocked item that says
    nothing, shown with a "No reason" chip until edited.
    """
    get = item.get if isinstance(item, dict) else (lambda key, default=None: getattr(item, key, default))
    # A scheduled item's blocked state is its own view (``scheduled_work_items``).
    if get("status") != "blocked" or get("action_mode"):
        return None
    parts: list[str] = []
    items = list(get("blocked_on") or [])
    if items:
        parts.append(f"{len(items)} work item(s)")
    pending = [r for r in get("blocked_on_refs") or [] if isinstance(r, dict) and not _satisfied(r)]
    if pending:
        parts.append(", ".join(waiting_label(r) for r in pending))
    reason = normalize_blocked_reason(get("blocked_reason"))
    if reason:
        parts.append(reason)
    if get("needs_human"):
        parts.append("a person (needs_human)")
    bound = bound_pull_request_wait(item)
    if bound:
        parts.insert(0, bound)
    if not parts:
        return {"text": "No reason given", "missing": True}
    return {"text": "Waits for: " + "; ".join(parts), "missing": False}


def bound_pull_request_wait(item: Any) -> str | None:
    """What releases a blocked *item* parked on its own open pull requests alone, ``None`` otherwise.

    The inbox finalizer parks an item on a draft it names with a reason only
    (#10692): the CI completion or the closing of that pull request hands it
    back (``work_item_pull_requests.parked_without_release``). Said in words,
    so a reader never takes it for a free-text block (#1617). Pure.
    """
    from app.services.work_item_pull_requests import is_draft, pull_request_bindings

    get = item.get if isinstance(item, dict) else (lambda key, default=None: getattr(item, key, default))
    if get("status") != "blocked" or get("blocked_on") or get("needs_human"):
        return None
    if open_refs(get("blocked_on_refs")):
        return None
    bound = [b for b in pull_request_bindings(get("payload")) if b.get("state", "open") == "open"]
    if not bound:
        return None
    names = ", ".join(f"{b['repo']}#{b['number']}{' (draft)' if is_draft(b) else ''}" for b in bound)
    return f"the CI completion or closing of {names}, released by itself"


# -- The matcher -----------------------------------------------------------------


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def closing_stamp(event: PullRequestEvent, now: datetime) -> str:
    """When the reference merged or closed, as the code host dates it; *now* when it does not.

    A release found by the polling fallback minutes after the merge still
    dates the merge, not its reading.
    """
    if event.closed_at:
        try:
            when = datetime.fromisoformat(event.closed_at.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            return _stamp(when if when.tzinfo else when.replace(tzinfo=UTC))
    return _stamp(now)


def _host_stamp(value: str | None) -> str | None:
    """*value* (the code host's ISO 8601 time) as ``YYYY-MM-DDTHH:MM:SSZ``; ``None`` when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _stamp(parsed)


def release_note(released: list[str], closed_at: str | None, now: datetime) -> str:
    """The note of an item released by *released*. Pure.

    ``released: <ref> merged at <when the host says it merged>`` and, when it
    differs, ``; released at <now>``; a host that states no closing time
    gives ``released: <ref> merged; released at <now>``: the release time is
    never shown as the merge time.
    """
    refs = ", ".join(released)
    stamp = _stamp(now)
    when = _host_stamp(closed_at)
    if when is None:
        return f"released: {refs}; released at {stamp}"
    if when == stamp:
        return f"released: {refs} at {when}"
    return f"released: {refs} at {when}; released at {stamp}"


def due_deferral(not_before: datetime | None, now: datetime) -> str:
    """The suffix of a release note when a future *not_before* defers the pickup. Pure.

    An inbox takes an item only from its ``not_before`` on
    (``list_dispatchable_work_items``; the ``due_at`` deadline defers nothing):
    a released item whose ``not_before`` is still ahead stays open and unclaimed
    until then, which the note says so nobody reads it as a stuck inbox.
    """
    if not_before is None:
        return ""
    at = not_before if not_before.tzinfo else not_before.replace(tzinfo=UTC)
    if at <= now:
        return ""
    return f"; the inbox takes it from its not_before ({_stamp(at)})"


def release_not_before(not_before: datetime | None, now: datetime) -> datetime:
    """The ``not_before`` of an item a closing releases. Pure.

    The run that merged a pull request usually completes the item waiting on it
    seconds later; an item released with no deferral was dispatched to a second
    run on the same delivery, which claimed it, so the merging run's completion
    was refused as a non-holder and the second run found nothing left to do.
    The release keeps the later of the item's own ``not_before`` and *now* plus
    :data:`RELEASE_GRACE_SECONDS`; nobody completing it, the inbox takes it then.
    """
    grace = now + timedelta(seconds=RELEASE_GRACE_SECONDS)
    if not_before is None:
        return grace
    at = not_before if not_before.tzinfo else not_before.replace(tzinfo=UTC)
    return max(at, grace)


def still_waiting_note(released: list[str], closed_at: str | None, now: datetime) -> str:
    """The note of an item some of whose references *released* satisfied. Pure."""
    refs = ", ".join(released)
    stamp = _stamp(now)
    when = _host_stamp(closed_at)
    if when is None:
        return f"{refs} (seen {stamp}); still waiting"
    return f"{refs} at {when} (seen {stamp}); still waiting"


def unmet_note(refused: list[str], pending: list[str], closed_at: str | None, now: datetime) -> str:
    """The note of an item a closing surfaced: a reference it can never satisfy. Pure.

    *refused* names the references closed the wrong way, *pending* the ones
    still open; the owner re-points ``blocked_on_refs`` (the successor pull
    request) or completes the item.
    """
    stamp = _stamp(now)
    when = _host_stamp(closed_at)
    at = f" at {when}" if when else ""
    still = f"; still pending: {', '.join(pending)}" if pending else ""
    return (
        f"surfaced: {'; '.join(refused)}{at}, so it can never release this item{still}; "
        f"re-point blocked_on_refs (the successor pull request) or complete it; surfaced at {stamp}"
    )


def _event_kind(event: PullRequestEvent) -> str | None:
    if event.kind == "closed":
        return "pull_request"
    if event.kind == "issue_closed":
        return "issue"
    return None


def followed_number(ref: dict, event: PullRequestEvent) -> int | None:
    """The pull request of *event* that *ref* waits for, ``None`` when none. Pure.

    Its own number, unless already seen closed unmerged; or, for a reference
    that carries ``head_branch``, a later pull request of the same repository
    whose head is that branch (a release pull request a workflow closes and
    opens again under a new number), not seen closed unmerged yet.
    """
    superseded = set(ref.get("superseded") or [])
    number = ref.get("number")
    if number in event.pr_numbers:
        return None if number in superseded else number
    branch = ref.get("head_branch")
    if not branch or event.kind != "closed" or event.head_branch != branch or not isinstance(number, int):
        return None
    for candidate in event.pr_numbers:
        if candidate > number and candidate not in superseded:
            return candidate
    return None


def apply_reference_event(
    refs: list[dict], event: PullRequestEvent, now: datetime
) -> tuple[list[dict], list[str], list[str]]:
    """*refs* after *event*: ``(refs, released, refused)``.

    *released* names each reference the event satisfied
    (``owner/name#12 merged``); *refused* each one it closed the wrong way (a
    pull request closed unmerged that a reference wanted merged). Pure.
    """
    kind = _event_kind(event)
    if kind is None:
        return refs, [], []
    outcome = "merged" if kind == "pull_request" and event.merged else "closed"
    released: list[str] = []
    refused: list[str] = []
    out: list[dict] = []
    for ref in refs:
        ref = dict(ref)
        if (
            ref.get("kind") == "deployment"
            and kind == "pull_request"
            and ref.get("repo") == event.repo
            and ref.get("number") in event.pr_numbers
            and not ref.get("merge_sha")
        ):
            if ref.get("carried_by") and not event.merged:
                # Carried from a work item's pull request (#9943): the item may
                # open a successor, and the dependency on it still holds; drop it.
                released.append(
                    f"{ref_label(ref)}: closed unmerged, dropped (carried from work item {ref['carried_by']})"
                )
                continue
            # The merge stage of a deployment: the deploy stage needs the merge commit.
            if event.merged and event.merge_sha:
                ref["merge_sha"] = event.merge_sha.lower()
                ref["state"] = "merged"
                ref["at"] = now.isoformat()
                if event.closed_at:
                    ref["merged_at"] = event.closed_at
                released.append(f"{ref_label(ref)}: merged {event.merge_sha[:7]}")
            else:
                refused.append(f"{ref_label(ref)}: {ref_label({**ref, 'kind': 'pull_request'})} closed unmerged")
            out.append(ref)
            continue
        number = followed_number(ref, event) if ref.get("kind") == kind else None
        if ref.get("repo") == event.repo and number is not None and not _satisfied(ref):
            wanted = ref.get("release_on")
            successor = f": successor #{number}" if number != ref.get("number") else ""
            if wanted == "merged_or_closed" or wanted == outcome:
                ref["state"] = outcome
                ref["at"] = now.isoformat()
                if successor:
                    ref["merged_number"] = number
                released.append(f"{ref_label(ref)}{successor} {outcome}")
            elif kind == "pull_request" and ref.get("head_branch"):
                # Superseded on its branch: wait for the successor instead of surfacing.
                ref["superseded"] = sorted({*(ref.get("superseded") or []), number})
                released.append(
                    f"{event.repo}#{number} closed unmerged, waiting for its successor on {ref['head_branch']}"
                )
            else:
                refused.append(f"{ref_label(ref)} {outcome}, release_on {wanted}")
        out.append(ref)
    return out, released, refused


def all_satisfied(refs: list[dict]) -> bool:
    return bool(refs) and all(_satisfied(r) for r in refs)


def record_closing_on_payload(payload: dict | None, event: PullRequestEvent, now: datetime) -> dict | None:
    """*payload* with the pull request *event* closed marked merged or closed, ``None`` when unchanged.

    A release read by the polling fallback or an unsigned delivery's read-back
    reaches only this module, not the payload bindings the webhook path
    updates; a binding left ``open`` made the inbox run's end park the item on
    the same merged pull request again, and the next poll released it again,
    once per pass. The binding now records the closing the release applied. Pure.
    """
    if event.kind != "closed":
        return None
    from app.services.work_item_pull_requests import apply_pull_request_event

    out, _ = apply_pull_request_event(payload, event, now)
    return out if out != (payload or {}) else None


# -- The release -----------------------------------------------------------------


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def concerns_event(refs: Any, event: PullRequestEvent) -> bool:
    """Whether one of *refs* may be moved by *event*: same repository, and its number or head branch. Pure.

    A coarse pre-filter of the candidates (the apply decides): a reference on the
    event's pull request or issue, or one following the event's head branch.
    """
    for ref in refs or []:
        if not isinstance(ref, dict) or ref.get("repo") != event.repo:
            continue
        if ref.get("number") in event.pr_numbers:
            return True
        if ref.get("head_branch") and event.head_branch and ref.get("head_branch") == event.head_branch:
            return True
    return False


async def _candidate_ids(db, event: PullRequestEvent) -> list[uuid.UUID]:
    rows = await db.execute(
        select(WorkItem.id, WorkItem.blocked_on_refs)
        .where(
            WorkItem.status == "blocked",
            WorkItem.blocked_on_refs.is_not(None),
            WorkItem.action_mode.is_(None),
            cast(WorkItem.blocked_on_refs, Text).ilike(f"%{_like_literal(event.repo)}%", escape="\\"),
        )
        .order_by(WorkItem.last_activity_at.asc())
        .limit(_CANDIDATE_SCAN)
    )
    return [row[0] for row in rows.all() if concerns_event(row[1], event)][:_MAX_ITEMS_PER_EVENT]


async def _apply_to_item(item_id: uuid.UUID, event: PullRequestEvent, now: datetime) -> str | None:
    """Apply *event* to one item, on its locked row. ``released`` / ``surfaced`` / ``noted`` / ``None``.

    ``surfaced``: a reference the closing can never satisfy any more (a pull
    request closed unmerged that it wanted merged) put the item back to
    ``open`` with :func:`unmet_note`, instead of a claim note on an item left
    blocked on a wait that cannot hold.
    """
    from app.core.database import async_session

    async with async_session() as db:
        item = (
            await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
        ).scalar_one_or_none()
        if item is None or item.status != "blocked" or not item.blocked_on_refs:
            return None
        refs, released, refused = apply_reference_event(list(item.blocked_on_refs), event, now)
        if not released and not refused:
            return None
        item.blocked_on_refs = refs or None
        payload = record_closing_on_payload(item.payload, event, now)
        if payload is not None:
            item.payload = payload
        notes = list(item.claim_notes or [])
        outcome = "noted"
        note: str | None = None
        if refused:
            pending = [
                waiting_label(r)
                for r in open_refs(refs)
                if not (r.get("repo") == event.repo and r.get("number") in event.pr_numbers)
            ]
            note = unmet_note(refused, pending, event.closed_at, now)
            previous = item.description or ""
            item.description = f"{note}\n\n{previous}" if previous else note
            notes.append({"at": now.isoformat(), "text": note})
            item.status = "open"
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            item.blocked_on_refs = None
            item.blocked_reason = None
            item.needs_human = None
            item.last_activity_at = now
            outcome = "surfaced"
        elif released and (all_satisfied(refs) or not refs) and not item.blocked_on:
            # ``not refs``: a carried reference dropped was its last wait.
            item.not_before = release_not_before(item.not_before, now)
            note = release_note(released, event.closed_at, now) + due_deferral(item.not_before, now)
            previous = item.description or ""
            item.description = f"{note}\n\n{previous}" if previous else note
            notes.append({"at": now.isoformat(), "text": note})
            item.status = "open"
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            item.blocked_on_refs = None
            item.blocked_reason = None
            item.needs_human = None
            item.last_activity_at = now
            outcome = "released"
        elif released:
            notes.append({"at": now.isoformat(), "text": still_waiting_note(released, event.closed_at, now)})
        item.claim_notes = notes
        await db.commit()
    if outcome == "released":
        await _publish_release(item_id, note or "", event, released)
    elif outcome == "surfaced":
        await _publish_surfaced(item_id, note or "")
    elif any(r.get("kind") == "deployment" and r.get("merge_sha") and not _satisfied(r) for r in refs):
        # Merged: the running build may already carry it (a late delivery).
        schedule_deployment_check(item_id)
    return outcome


def apply_dequeue_event(refs: list[dict[str, Any]], event: PullRequestEvent) -> tuple[list[dict[str, Any]], list[str]]:
    """*refs* after a merge queue removal *event*: ``(refs, removed)``. Pure.

    *removed* names each open reference waiting for the merge of the pull
    request the queue dropped (a pull request, or the merge stage of a
    deployment). The removal is stamped on the reference (``dequeued_at``), so a
    redelivery names nothing and a later removal of the same pull request does.
    """
    if event.kind != "dequeued":
        return refs, []
    stamp = event.closed_at or event.conclusion or "removed"
    removed: list[str] = []
    out: list[dict[str, Any]] = []
    for ref in refs:
        ref = dict(ref)
        waits_merge = (ref.get("kind") == "pull_request" and ref.get("release_on") != "closed") or (
            ref.get("kind") == "deployment" and not ref.get("merge_sha")
        )
        if (
            waits_merge
            and ref.get("repo") == event.repo
            and ref.get("number") in event.pr_numbers
            and not _satisfied(ref)
            and ref.get("dequeued_at") != stamp
        ):
            ref["dequeued_at"] = stamp
            removed.append(ref_label(ref))
        out.append(ref)
    return out, removed


def removal_after(removed_at: str | None, last_activity_at: datetime | None) -> bool:
    """Whether a removal stamped *removed_at* came after *last_activity_at*. Pure; unreadable is ``False``."""
    when = _parse_stamp(removed_at)
    if when is None:
        return False
    if last_activity_at is None:
        return True
    since = last_activity_at if last_activity_at.tzinfo else last_activity_at.replace(tzinfo=UTC)
    return when > since


def dequeued_note(removed: list[str], event: PullRequestEvent, now: datetime, *, still_blocked: bool) -> str:
    """The note of an item whose pull request the merge queue dropped. Pure."""
    from app.services.work_item_pull_requests import DEQUEUE_HINTS

    when = _host_stamp(event.closed_at)
    at = f" at {when}" if when else ""
    reason = event.conclusion or "unknown"
    hint = DEQUEUE_HINTS.get(reason)
    if hint:
        reason = f"{reason}; {hint}"
    run_hint = (event.extra or {}).get("queue_run_hint")
    if isinstance(run_hint, str) and run_hint:
        reason = f"{reason}; {run_hint}"
    tail = (
        "it stays blocked until the items it waits for are done"
        if still_blocked
        else "back in the queue: fix the pull request and enqueue it again, or re-point blocked_on_refs"
    )
    return (
        f"surfaced: {', '.join(removed)} removed from the merge queue without a merge ({reason}){at}, "
        f"so no closing will release this item; {tail}; surfaced at {_stamp(now)}"
    )


async def _apply_dequeue_to_item(
    item_id: uuid.UUID, event: PullRequestEvent, now: datetime, *, polled: bool = False
) -> str | None:
    """Apply a merge queue removal to one item, on its locked row. ``surfaced`` / ``noted`` / ``None``.

    *polled*: the removal was read from the pull request's timeline, not delivered;
    it counts only when it is newer than the item's last activity (a removal the
    item was re-parked after is history, #606).
    """
    from app.core.database import async_session

    async with async_session() as db:
        item = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None or item.status != "blocked" or not item.blocked_on_refs:
            return None
        if polled and not removal_after(event.closed_at, item.last_activity_at):
            return None
        refs, removed = apply_dequeue_event(list(item.blocked_on_refs), event)
        if not removed:
            return None
        item.blocked_on_refs = refs
        still_blocked = bool(item.blocked_on)
        note = dequeued_note(removed, event, now, still_blocked=still_blocked)
        item.claim_notes = [*(item.claim_notes or []), {"at": now.isoformat(), "text": note}]
        item.last_activity_at = now
        if still_blocked:
            await db.commit()
            return "noted"
        previous = item.description or ""
        item.description = f"{note}\n\n{previous}" if previous else note
        # The references stay: the item re-parks on them once the pull request is enqueued again.
        item.status = "open"
        item.claimed_by_agent_id = None
        item.claimed_by_run_id = None
        item.claimed_at = None
        item.blocked_reason = None
        item.needs_human = None
        await db.commit()
    await _publish_surfaced(item_id, note)
    return "surfaced"


async def surface_on_dequeue(event: PullRequestEvent) -> dict[str, int]:
    """Surface the blocked items waiting for the merge of a pull request the merge queue dropped.

    GitHub sends no closing when its merge queue removes a pull request without
    merging it: an item blocked on that merge through ``blocked_on_refs`` alone
    would wait forever (#397). ``{"surfaced": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session

    counts = {"surfaced": 0, "noted": 0}
    if event.kind != "dequeued":
        return counts
    now = datetime.now(UTC)
    try:
        async with async_session() as db:
            ids = await _candidate_ids(db, event)
    except Exception as exc:
        logger.warning("Blocked references on %s unreadable: %s", event.repo, format_exception_message(exc))
        return counts
    for item_id in ids:
        try:
            outcome = await _apply_dequeue_to_item(item_id, event, now)
        except Exception as exc:
            logger.warning(
                "Merge queue removal on %s could not move work item %s: %s",
                event.repo,
                item_id,
                format_exception_message(exc),
            )
            continue
        if outcome:
            counts[outcome] += 1
    if any(counts.values()):
        logger.info("Merge queue removal on %s: %s", event.repo, counts)
    return counts


async def _publish_surfaced(item_id: uuid.UUID, note: str) -> None:
    """``work_item.status_changed`` (blocked to open) of an item a closing surfaced. Best-effort."""
    from app.services.work_item import publish_transition

    try:
        await publish_transition(item_id, previous_status="blocked", note=note)
    except Exception as exc:  # best-effort: the surfacing already landed
        logger.warning("Surfaced work item %s not published: %s", item_id, format_exception_message(exc))


async def _publish_release(item_id: uuid.UUID, note: str, event: PullRequestEvent, released: list[str]) -> None:
    from app.core.database import async_session
    from app.services.platform_events import publish, work_item_data
    from app.services.work_item import publish_transition

    await publish_transition(item_id, previous_status="blocked", note=note)
    try:
        async with async_session() as db:
            item = await db.get(WorkItem, item_id)
        if item is None:
            return
        data = work_item_data(item, previous_status="blocked", note=note)
        data["released_by"] = {
            "repo": event.repo,
            "kind": _event_kind(event),
            "numbers": list(event.pr_numbers),
            "merged": bool(event.merged),
            "references": released,
        }
        await publish("work_item.released", data)
    except Exception as exc:  # best-effort: the release already landed
        logger.warning("work_item.released for %s not published: %s", item_id, format_exception_message(exc))


async def release_blocked_on_refs(event: PullRequestEvent) -> dict:
    """Release the blocked items whose references *event* satisfies.

    ``{"released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session

    if _event_kind(event) is None:
        return {"released": 0, "noted": 0}
    now = datetime.now(UTC)
    try:
        async with async_session() as db:
            ids = await _candidate_ids(db, event)
    except Exception as exc:
        logger.warning("Blocked references on %s unreadable: %s", event.repo, format_exception_message(exc))
        return {"released": 0, "noted": 0}
    counts = {"released": 0, "noted": 0}
    for item_id in ids:
        try:
            outcome = await _apply_to_item(item_id, event, now)
        except Exception as exc:
            logger.warning(
                "Reference event on %s could not move work item %s: %s",
                event.repo,
                item_id,
                format_exception_message(exc),
            )
            continue
        if outcome:
            # ``surfaced`` appears only when an item was surfaced.
            counts[outcome] = counts.get(outcome, 0) + 1
    if any(counts.values()):
        logger.info("Reference event %s on %s: %s", event.kind, event.repo, counts)
    return counts


# -- An unsigned delivery, read back -------------------------------------------

#: (repo, kind, number) -> monotonic time of its last read-back, per process: an
#: unsigned delivery storm reads one reference at most once per window.
_hint_reads: dict[tuple, float] = {}
HINT_QUIET_SECONDS = 20.0
#: Provider reads one unsigned delivery may make: it is read inline, in the
#: webhook request, so a closing named by many owners stays bounded.
HINT_MAX_READS = 5


async def _hint_candidates(
    db: Any, event: PullRequestEvent, matches: Any = None
) -> list[tuple[uuid.UUID, Any, list[Any]]]:
    """The blocked items whose references *matches* (default :func:`_named_by`) finds named by *event*."""
    named = matches or _named_by
    rows = await db.execute(
        select(WorkItem.id, WorkItem.user_id, WorkItem.blocked_on_refs)
        .where(
            WorkItem.status == "blocked",
            WorkItem.blocked_on_refs.is_not(None),
            WorkItem.action_mode.is_(None),
            WorkItem.user_id.is_not(None),
            cast(WorkItem.blocked_on_refs, Text).ilike(f"%{_like_literal(event.repo)}%", escape="\\"),
        )
        .order_by(WorkItem.last_activity_at.asc())
        .limit(_CANDIDATE_SCAN)
    )
    return [(row[0], row[1], list(row[2] or [])) for row in rows.all() if named(list(row[2] or []), event)][
        :_MAX_ITEMS_PER_EVENT
    ]


def _named_by(refs: list, event: PullRequestEvent) -> list[dict]:
    kind = _event_kind(event)
    return [
        r
        for r in open_refs(refs)
        if r.get("repo") == event.repo and read_kind(r) == kind and _read_number(r, event) is not None
    ]


def _read_number(ref: dict, event: PullRequestEvent) -> int | None:
    """The number of *event* to read back for *ref*: its own, or a successor on its head branch. Pure."""
    if ref.get("number") in event.pr_numbers:
        return int(ref["number"])
    if ref.get("kind") == "pull_request":
        return followed_number(ref, event)
    return None


def read_kind(ref: dict) -> str | None:
    """The kind the code host's reader reads *ref* as; ``None`` when it reads nothing.

    A deployment reference reads as its pull request until the merge is known;
    its deploy stage is never polled. Pure.
    """
    if ref.get("kind") == "deployment":
        return "pull_request" if ref.get("number") and not ref.get("merge_sha") else None
    if ref.get("kind") in ("workflow_run", "tag", "a2a_task"):
        # Read by the workflow run reader (:func:`_poll_workflow_run`), the tag reader
        # (:func:`_poll_tag`) or the A2A task pass (``work_item_a2a_refs``), never as a pull request.
        return None
    return ref.get("kind")


async def release_from_unverified_delivery(event: PullRequestEvent) -> dict:
    """Release the items an UNSIGNED closing delivery names, once the code host confirms it.

    A delivery accepted without a webhook secret is not trusted with a work
    item's state, but it says which reference changed: each one an
    item waits for is read back from the provider with the credential its
    owner reads that provider with (the polling fallback's reader), and only
    the closing that read returns is applied. The release then lands within
    seconds of the merge instead of the next polling pass. Each reference is
    read at most once per :data:`HINT_QUIET_SECONDS` per process.

    ``{"read": r, "released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import reference_state_readers

    counts = {"read": 0, "released": 0, "noted": 0}
    if _event_kind(event) is None:
        return counts
    readers = reference_state_readers()
    if not readers:
        return counts
    now_mono = time.monotonic()
    try:
        async with async_session() as db:
            candidates = [c for c in await _hint_candidates(db, event) if _named_by(c[2], event)]
            tokens: dict[tuple[str, str], str | None] = {}
            for _, user_id, _ in candidates:
                for provider in readers:
                    key = (provider, str(user_id))
                    if key not in tokens:
                        tokens[key] = await reference_token(db, provider, user_id)
    except Exception as exc:
        logger.warning("Unsigned reference event on %s not read back: %s", event.repo, format_exception_message(exc))
        return counts
    seen: dict[tuple, PullRequestEvent | None] = {}
    read_now: set[tuple] = set()
    for item_id, user_id, refs in candidates:
        for ref in _named_by(refs, event):
            for provider, reader in readers.items():
                token = tokens.get((provider, str(user_id)))
                if not token:
                    continue
                number = _read_number(ref, event) or int(ref["number"])
                ref_key = (ref.get("repo"), read_kind(ref), number)
                key = (provider, str(user_id), *ref_key)
                if key not in seen:
                    last = _hint_reads.get(ref_key)
                    if ref_key not in read_now and last is not None and now_mono - last < HINT_QUIET_SECONDS:
                        # A duplicate delivery of the same closing: already read back.
                        seen[key] = None
                    elif counts["read"] >= HINT_MAX_READS:
                        break
                    else:
                        counts["read"] += 1
                        read_now.add(ref_key)
                        _hint_reads[ref_key] = now_mono
                        try:
                            seen[key] = await reader(str(ref["repo"]), str(read_kind(ref)), number, token)
                        except Exception as exc:
                            logger.info("Reference %s not read: %s", ref_label(ref), format_exception_message(exc))
                            seen[key] = None
                verified = seen[key]
                if verified is None:
                    continue
                try:
                    outcome = await _apply_to_item(item_id, verified, datetime.now(UTC))
                except Exception as exc:
                    logger.warning(
                        "Read-back reference %s could not move work item %s: %s",
                        ref_label(ref),
                        item_id,
                        format_exception_message(exc),
                    )
                    continue
                if outcome:
                    counts[outcome] = counts.get(outcome, 0) + 1
                break
    if len(_hint_reads) > POLL_MAX_ITEMS * 40:
        for stale in [k for k, t in _hint_reads.items() if now_mono - t >= HINT_QUIET_SECONDS]:
            _hint_reads.pop(stale, None)
    if counts["read"]:
        logger.info("Unsigned reference event %s on %s read back: %s", event.kind, event.repo, counts)
    return counts


# -- The deploy stage ------------------------------------------------------

#: (provider, repo, merge_sha, build_sha) -> contained, per process: a build never changes.
_containment: dict[tuple[str, str, str, str], bool] = {}
_CONTAINMENT_MAX = 2000
#: Items one release event may check.
_MAX_ITEMS_PER_RELEASE = 200
#: Running checks, kept so the loop does not drop them.
_checks: set[asyncio.Task] = set()


def deployed_note(ref: dict, version: str, first_seen_at: str | None, now: datetime) -> str:
    """The note of an item released by the deployment of *ref*. Pure."""
    merged = f" (merged {str(ref.get('merge_sha') or '')[:7]})" if ref.get("number") else ""
    seen = _host_stamp(first_seen_at)
    in_build = f"in build {version}" + (f" at {seen}" if seen else "")
    return f"released: {ref_label(ref)}{merged} {in_build}; released at {_stamp(now)}"


def pending_deployments(refs: Any) -> list[dict]:
    """The deployment references of *refs* merged and not yet deployed. Pure."""
    return [
        r
        for r in (refs or [])
        if isinstance(r, dict) and r.get("kind") == "deployment" and r.get("merge_sha") and not _satisfied(r)
    ]


def same_commit(a: str, b: str) -> bool:
    """Whether *a* and *b* name the same commit: equal, or one a prefix of the other, 7 characters at least. Pure."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


async def _contained(
    db, user_id: Any, ref: dict, build_sha: str, tokens: dict[tuple[str, str], str | None]
) -> bool | None:
    """Whether the build *build_sha* carries *ref*'s merge commit; ``None`` when unreadable."""
    from app.integrations.reference_state import commit_containment_readers

    merge_sha = str(ref.get("merge_sha") or "")
    repo = str(ref.get("repo") or "")
    if same_commit(merge_sha, build_sha):
        # The build is the merge commit itself: contained, nothing to read.
        return True
    for provider, reader in commit_containment_readers().items():
        key = (provider, repo, merge_sha, build_sha)
        if key in _containment:
            return _containment[key]
        token_key = (provider, str(user_id))
        if token_key not in tokens:
            tokens[token_key] = await reference_token(db, provider, user_id)
        token = tokens[token_key]
        if not token:
            continue
        try:
            answer = await reader(repo, merge_sha, build_sha, token)
        except Exception as exc:
            logger.info("Deployment of %s not read: %s", ref_label(ref), format_exception_message(exc))
            answer = None
        if answer is None:
            continue
        if len(_containment) >= _CONTAINMENT_MAX:
            _containment.clear()
        _containment[key] = answer
        return answer
    return None


async def _apply_deployment(item_id: uuid.UUID, release: dict, now: datetime) -> str | None:
    """Check the deployment references of one item against *release*. ``released`` / ``noted`` / ``None``."""
    from app.core.database import async_session

    build_sha = str(release.get("git_sha") or "").lower()
    version = str(release.get("version") or "")
    if not build_sha:
        return None
    tokens: dict[tuple[str, str], str | None] = {}
    async with async_session() as db:
        row = (
            await db.execute(select(WorkItem.user_id, WorkItem.blocked_on_refs).where(WorkItem.id == item_id))
        ).one_or_none()
        if row is None or row[0] is None:
            return None
        verdicts: dict[tuple, bool | None] = {}
        for ref in pending_deployments(row[1]):
            verdicts[_ref_key(ref)] = await _contained(db, row[0], ref, build_sha, tokens)
    if not verdicts:
        return None
    async with async_session() as db:
        item = (
            await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
        ).scalar_one_or_none()
        if item is None or item.status != "blocked" or not item.blocked_on_refs:
            return None
        refs: list[dict] = []
        deployed: list[dict] = []
        changed = False
        for ref in item.blocked_on_refs:
            ref = dict(ref)
            if ref.get("kind") == "deployment" and ref.get("merge_sha") and not _satisfied(ref):
                verdict = verdicts.get(_ref_key(ref))
                if verdict:
                    ref["state"] = "deployed"
                    ref["at"] = now.isoformat()
                    ref["build"] = version
                    deployed.append(ref)
                    changed = True
                elif verdict is False and ref.get("build") != version:
                    # Read and not in this build: the Waits for line names it.
                    ref["build"] = version
                    changed = True
            refs.append(ref)
        if not changed:
            return None
        item.blocked_on_refs = refs
        outcome = "noted"
        note: str | None = None
        if deployed:
            notes = list(item.claim_notes or [])
            texts = [deployed_note(r, version, release.get("first_seen_at"), now) for r in deployed]
            if all_satisfied(refs) and not item.blocked_on:
                note = "\n".join(texts) + due_deferral(item.not_before, now)
                previous = item.description or ""
                item.description = f"{note}\n\n{previous}" if previous else note
                notes.append({"at": now.isoformat(), "text": note})
                item.status = "open"
                item.claimed_by_agent_id = None
                item.claimed_by_run_id = None
                item.claimed_at = None
                item.blocked_on_refs = None
                item.blocked_reason = None
                item.needs_human = None
                item.last_activity_at = now
                outcome = "released"
            else:
                notes.extend({"at": now.isoformat(), "text": f"{t}; still waiting"} for t in texts)
            item.claim_notes = notes
        await db.commit()
    if outcome == "released":
        await _publish_deployed(item_id, note or "", release, deployed)
    return outcome


async def _publish_deployed(item_id: uuid.UUID, note: str, release: dict, deployed: list[dict]) -> None:
    from app.core.database import async_session
    from app.services.platform_events import publish, work_item_data
    from app.services.work_item import publish_transition

    await publish_transition(item_id, previous_status="blocked", note=note)
    try:
        async with async_session() as db:
            item = await db.get(WorkItem, item_id)
        if item is None:
            return
        data = work_item_data(item, previous_status="blocked", note=note)
        data["released_by"] = {
            "kind": "deployment",
            "version": release.get("version"),
            "git_sha": release.get("git_sha"),
            "references": [ref_label(r) for r in deployed],
            "merge_shas": [r.get("merge_sha") for r in deployed],
        }
        await publish("work_item.released", data)
    except Exception as exc:  # best-effort: the release already landed
        logger.warning("work_item.released for %s not published: %s", item_id, format_exception_message(exc))


async def _deployment_candidates(db) -> list[uuid.UUID]:
    rows = await db.execute(
        select(WorkItem.id, WorkItem.blocked_on_refs)
        .where(
            WorkItem.status == "blocked",
            WorkItem.blocked_on_refs.is_not(None),
            WorkItem.action_mode.is_(None),
            WorkItem.user_id.is_not(None),
            cast(WorkItem.blocked_on_refs, Text).ilike('%"deployment"%'),
        )
        .order_by(WorkItem.last_activity_at.asc())
        .limit(_MAX_ITEMS_PER_RELEASE)
    )
    return [row[0] for row in rows.all() if pending_deployments(row[1])]


async def on_release_deployed(release: dict) -> dict:
    """Release the items whose merged change the new build *release* carries.

    *release* is the ``platform.release.deployed`` payload (``version``,
    ``git_sha``, ``first_seen_at``). ``{"released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.services.platform_builds import resolve_build_sha

    counts = {"released": 0, "noted": 0}
    # A pod started without GIT_SHA raises the event with git_sha null: the build's
    # commit is then read from its recorded row or its version.
    git_sha = await resolve_build_sha(str(release.get("version") or ""), release.get("git_sha"))
    if not git_sha:
        return counts
    release = {**release, "git_sha": git_sha}
    try:
        async with async_session() as db:
            ids = await _deployment_candidates(db)
    except Exception as exc:
        logger.warning("Deployment references not read: %s", format_exception_message(exc))
        return counts
    now = datetime.now(UTC)
    for item_id in ids:
        try:
            outcome = await _apply_deployment(item_id, release, now)
        except Exception as exc:
            logger.warning("Deployment check could not move work item %s: %s", item_id, format_exception_message(exc))
            continue
        if outcome:
            counts[outcome] += 1
    if counts["released"] or counts["noted"]:
        logger.info("Release %s: deployment references %s", release.get("version"), counts)
    return counts


async def running_release() -> dict:
    """The running build as the release event names it (``version``, ``git_sha``).

    The commit falls back to the one recorded for the running version, then to
    the one a ``-dev.<sha>`` version names, when the process has no ``GIT_SHA``.
    """
    from app.services.platform_builds import resolve_build_sha, running_build

    version, git_sha = running_build()
    resolved = await resolve_build_sha(version, git_sha)
    return {"version": version, "git_sha": resolved or None, "first_seen_at": None}


async def _read_merge_stage(item_id: uuid.UUID) -> None:
    """Read the pull requests of the item's deployment references not known merged (a merge before the write)."""
    from app.core.database import async_session
    from app.integrations.reference_state import reference_state_readers

    readers = reference_state_readers()
    async with async_session() as db:
        row = (
            await db.execute(select(WorkItem.user_id, WorkItem.blocked_on_refs).where(WorkItem.id == item_id))
        ).one_or_none()
        if row is None or row[0] is None:
            return
        waiting = [r for r in open_refs(row[1]) if r.get("kind") == "deployment" and read_kind(r)]
        tokens = {p: await reference_token(db, p, row[0]) for p in readers} if waiting else {}
    for ref in waiting[:POLL_MAX_ITEMS]:
        for provider, reader in readers.items():
            if not tokens.get(provider):
                continue
            event = await reader(str(ref["repo"]), "pull_request", int(ref["number"]), str(tokens[provider]))
            if event is not None:
                await _apply_to_item(item_id, event, datetime.now(UTC))
            break


async def check_item_deployment(item_id: uuid.UUID | str) -> str | None:
    """The check made once at the write: the pull request may have merged and the running
    build may already carry it. Never raises."""
    try:
        await _read_merge_stage(uuid.UUID(str(item_id)))
        return await _apply_deployment(uuid.UUID(str(item_id)), await running_release(), datetime.now(UTC))
    except Exception as exc:
        logger.warning("Deployment check of work item %s failed: %s", item_id, format_exception_message(exc))
        return None


def _spawn(coro: Any) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop: nothing to schedule on
        coro.close()
        return
    _checks.add(task)
    task.add_done_callback(_checks.discard)


def schedule_deployment_check(item_id: uuid.UUID | str) -> None:
    """Check *item_id*'s deployment references against the running build, in the background."""
    _spawn(check_item_deployment(item_id))


def schedule_release_check(release: dict) -> None:
    """Run :func:`on_release_deployed` in the background (a startup step must not wait on GitHub)."""
    _spawn(on_release_deployed(dict(release)))


# -- The workflow run stage --------------------------------------------------------


def run_commit(run: dict[str, Any]) -> str | None:
    """The commit a release run's display title names (``Release <ref> at <sha>``), else ``None``. Pure."""
    match = _TITLE_SHA_RE.search(str(run.get("display_title") or "").lower())
    return match.group(1) if match else None


def _one_second_later(value: object) -> str | None:
    """*value* (a run's ``created_at``) plus one second, ``YYYY-MM-DDTHH:MM:SSZ``; ``None`` when unreadable."""
    stamp = _host_stamp(value) if isinstance(value, str) else None
    if stamp is None:
        return None
    return _stamp(datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(seconds=1))


async def _carrying_run(ref: dict[str, Any], run: dict[str, Any], provider: str, token: str) -> bool | None:
    """Whether *run* releases a commit that is ``carries_sha`` or a descendant of it.

    ``False`` when its title names no commit or an older one, ``None`` when the
    containment cannot be read (#1634).
    """
    from app.integrations.reference_state import commit_containment_readers

    commit = run_commit(run)
    if commit is None:
        return False
    merge_sha = str(ref.get("carries_sha") or "")
    if same_commit(merge_sha, commit):
        return True
    reader = commit_containment_readers().get(provider)
    if reader is None:
        return None
    repo = str(ref.get("carries_repo") or ref.get("repo") or "")
    try:
        return await reader(repo, merge_sha, commit, token)
    except Exception as exc:
        logger.info("Release of %s not read: %s", ref_label(ref), format_exception_message(exc))
        return None


def workflow_run_note(ref: dict, run: dict, now: datetime) -> str:
    """The note of an item a successful workflow run released. Pure."""
    url = f" ({run['url']})" if run.get("url") else ""
    when = _host_stamp(run.get("updated_at")) or _host_stamp(run.get("created_at"))
    at = f" at {when}" if when else ""
    return f"released: {ref_label(ref)}: run {run.get('id')}{url} succeeded{at}; released at {_stamp(now)}"


async def _apply_workflow_run(item_id: uuid.UUID, key: tuple, run: dict, now: datetime) -> str | None:
    """Mark the ``workflow_run`` reference *key* of one item succeeded by *run*. ``released`` / ``noted`` / ``None``."""
    from app.core.database import async_session

    async with async_session() as db:
        item = (
            await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
        ).scalar_one_or_none()
        if item is None or item.status != "blocked" or not item.blocked_on_refs:
            return None
        refs: list[dict] = []
        matched: list[dict] = []
        for ref in item.blocked_on_refs:
            ref = dict(ref)
            if ref.get("kind") == "workflow_run" and _ref_key(ref) == key and not _satisfied(ref):
                ref["state"] = "succeeded"
                ref["at"] = now.isoformat()
                for field, value in (
                    ("run_id", run.get("id")),
                    ("run_url", run.get("url")),
                    ("run_at", run.get("created_at")),
                    ("run_commit", run.get("run_commit")),
                ):
                    if value is not None:
                        ref[field] = value
                matched.append(ref)
            refs.append(ref)
        if not matched:
            return None
        item.blocked_on_refs = refs
        notes = list(item.claim_notes or [])
        texts = [workflow_run_note(r, run, now) for r in matched]
        outcome = "noted"
        note: str | None = None
        if all_satisfied(refs) and not item.blocked_on:
            note = "\n".join(texts) + due_deferral(item.not_before, now)
            previous = item.description or ""
            item.description = f"{note}\n\n{previous}" if previous else note
            notes.append({"at": now.isoformat(), "text": note})
            item.status = "open"
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            item.blocked_on_refs = None
            item.blocked_reason = None
            item.needs_human = None
            item.last_activity_at = now
            outcome = "released"
        else:
            notes.extend({"at": now.isoformat(), "text": f"{t}; still waiting"} for t in texts)
        item.claim_notes = notes
        await db.commit()
    if outcome == "released":
        await _publish_workflow_run(item_id, note or "", run, matched)
    return outcome


async def _publish_workflow_run(item_id: uuid.UUID, note: str, run: dict, matched: list[dict]) -> None:
    from app.core.database import async_session
    from app.services.platform_events import publish, work_item_data
    from app.services.work_item import publish_transition

    await publish_transition(item_id, previous_status="blocked", note=note)
    try:
        async with async_session() as db:
            item = await db.get(WorkItem, item_id)
        if item is None:
            return
        data = work_item_data(item, previous_status="blocked", note=note)
        data["released_by"] = {
            "kind": "workflow_run",
            "run_id": run.get("id"),
            "run_url": run.get("url"),
            "references": [ref_label(r) for r in matched],
        }
        await publish("work_item.released", data)
    except Exception as exc:  # best-effort: the release already landed
        logger.warning("work_item.released for %s not published: %s", item_id, format_exception_message(exc))


async def _poll_workflow_run(
    item_id: uuid.UUID,
    user_id: Any,
    ref: dict,
    readers: dict,
    tokens: dict[tuple[str, str], str | None],
    seen: dict[tuple, dict | None],
    counts: dict,
    max_reads: int = POLL_MAX_READS,
) -> bool:
    """Read one ``workflow_run`` reference through the providers' readers and apply a successful run.

    False when the read budget (*max_reads* per pass or delivery) left the reference
    unread, so the item is not stamped polled. A ``carries_sha`` reference reads the
    next run while the one found does not carry the merge (:func:`_carrying_run`),
    from ``carry_after`` when an earlier read skipped older runs, and records the
    position it reached when it ends without a carrying run (:func:`_record_carry_after`).
    """
    key = _ref_key(ref)
    for provider, reader in readers.items():
        token = tokens.get((provider, str(user_id)))
        if not token:
            continue
        after = str(ref.get("carry_after") or ref["after"])
        start = after
        run: dict[str, Any] | None = None
        for _ in range(MAX_CARRY_SKIPS + 1):
            read_key = (provider, str(user_id), *key, after)
            if read_key not in seen:
                if counts["read"] >= max_reads:
                    return False
                counts["read"] += 1
                try:
                    seen[read_key] = await reader(
                        str(ref["repo"]),
                        str(ref["workflow"]),
                        after,
                        ref.get("branch"),
                        ref.get("title_contains"),
                        token,
                    )
                except Exception as exc:
                    _note_read_failure(counts, ref, exc, item_id)
                    seen[read_key] = None
            run = seen[read_key]
            if run is None or not ref.get("carries_sha"):
                break
            carried = await _carrying_run(ref, run, provider, token)
            if carried is True:
                run = {**run, "run_commit": run_commit(run)}
                break
            if carried is None:
                # Unreadable: wait for the next pass rather than release on a guess.
                run = None
                break
            # An older commit's release: the next successful run may carry the merge.
            later = _one_second_later(run.get("created_at"))
            if later is None:
                run = None
                break
            after, run = later, None
        if run is None:
            if ref.get("carries_sha") and after != start:
                await _record_carry_after(item_id, key, after)
            continue
        try:
            outcome = await _apply_workflow_run(item_id, key, run, datetime.now(UTC))
        except Exception as exc:
            logger.warning(
                "Polled reference %s could not move work item %s: %s",
                ref_label(ref),
                item_id,
                format_exception_message(exc),
            )
            return True
        if outcome:
            counts[outcome] = counts.get(outcome, 0) + 1
        return True
    return True


async def _record_carry_after(item_id: uuid.UUID, key: tuple, after: str) -> None:
    """Keep *after* as ``carry_after`` on the ``workflow_run`` reference *key* of one item (#1634).

    The runs before it are older commits' releases already skipped: without it a
    reference with more of them than one read skips would re-read the same ones on
    every pass and never reach the carrying release. Best-effort.
    """
    from app.core.database import async_session

    try:
        async with async_session() as db:
            item = (
                await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
            ).scalar_one_or_none()
            if item is None or item.status != "blocked" or not item.blocked_on_refs:
                return
            refs: list[dict] = []
            moved = False
            for ref in item.blocked_on_refs:
                ref = dict(ref)
                if (
                    ref.get("kind") == "workflow_run"
                    and _ref_key(ref) == key
                    and not _satisfied(ref)
                    and str(ref.get("carry_after") or ref.get("after") or "") < after
                ):
                    ref["carry_after"] = after
                    moved = True
                refs.append(ref)
            if not moved:
                return
            item.blocked_on_refs = refs
            await db.commit()
    except Exception as exc:
        logger.info("Carry position of work item %s not kept: %s", item_id, format_exception_message(exc))


async def _poll_branch_successors(
    item_id: uuid.UUID,
    user_id: Any,
    ref: dict,
    readers: dict,
    tokens: dict[tuple[str, str], str | None],
    seen: dict[tuple, list[PullRequestEvent]],
    counts: dict,
) -> None:
    """Apply the closings of the later pull requests of *ref*'s head branch, oldest first.

    A reference that carries ``head_branch`` waits for the pull request it names
    or a successor on that branch; the numbered read misses a successor, so the
    branch's closed pull requests are read once per pass and each later one is
    applied as its delivery would have been (:func:`followed_number`).
    """
    for provider, reader in readers.items():
        token = tokens.get((provider, str(user_id)))
        if not token:
            continue
        key = (provider, str(user_id), ref.get("repo"), ref.get("head_branch"))
        if key not in seen:
            if counts["read"] >= POLL_MAX_READS:
                return
            counts["read"] += 1
            try:
                seen[key] = list(await reader(str(ref["repo"]), str(ref["head_branch"]), token))
            except Exception as exc:
                logger.info("Branch of %s not read: %s", ref_label(ref), format_exception_message(exc))
                seen[key] = []
        for event in seen[key]:
            if not event.pr_numbers or event.pr_numbers[0] <= int(ref.get("number") or 0):
                continue
            try:
                outcome = await _apply_to_item(item_id, event, datetime.now(UTC))
            except Exception as exc:
                logger.warning(
                    "Polled successor of %s could not move work item %s: %s",
                    ref_label(ref),
                    item_id,
                    format_exception_message(exc),
                )
                return
            if outcome:
                counts[outcome] = counts.get(outcome, 0) + 1
            if outcome in ("released", "surfaced"):
                return
        return


# -- The workflow run delivery -----------------------------------------------------


def delivered_workflow_names(event: PullRequestEvent) -> frozenset[str]:
    """The names a ``workflow_run`` reference may give the workflow of *event*: its file, its id. Pure.

    Empty unless *event* is a completed, successful workflow run (the ``ci``
    form of a GitHub ``workflow_run`` delivery): a check suite, a run still going
    or a failed run releases nothing.
    """
    if event.kind != "ci" or event.status != "completed" or event.conclusion != "success":
        return frozenset()
    if not event.run_key.startswith("workflow:"):
        return frozenset()
    names = {event.run_key.removeprefix("workflow:")}
    workflow_file = (event.extra or {}).get("workflow_file")
    if isinstance(workflow_file, str) and workflow_file:
        names.add(workflow_file)
    return frozenset(n for n in names if n)


def named_by_workflow_run(refs: list[Any], event: PullRequestEvent) -> list[dict[str, Any]]:
    """The open ``workflow_run`` references of *refs* the successful run *event* may satisfy. Pure.

    Same repository, same workflow (file name or id), same branch when the
    reference names one. The time and title filters are the reader's: the run is
    read back from the code host before anything is applied.
    """
    names = delivered_workflow_names(event)
    if not names:
        return []
    return [
        r
        for r in open_refs(refs)
        if r.get("kind") == "workflow_run"
        and r.get("repo") == event.repo
        and str(r.get("workflow")) in names
        and (not r.get("branch") or r.get("branch") == event.head_branch)
    ]


async def release_from_workflow_run_delivery(event: PullRequestEvent) -> dict[str, Any]:
    """Release the items waiting on the workflow run a successful ``workflow_run`` delivery reports.

    The delivery, signed or not, only says which reference may be met: each
    ``workflow_run`` reference it names (:func:`named_by_workflow_run`) is read
    back through the polling fallback's reader with its owner's credential, and
    only the run that read returns is applied. The release lands within seconds
    of the run instead of the next polling pass; the poller stays the fallback.
    At most :data:`HINT_MAX_READS` reads per delivery.

    ``{"read": r, "released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import workflow_run_readers

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    if not delivered_workflow_names(event):
        return counts
    readers = workflow_run_readers()
    if not readers:
        return counts
    try:
        async with async_session() as db:
            candidates = [
                c
                for c in await _hint_candidates(db, event, named_by_workflow_run)
                if named_by_workflow_run(c[2], event)
            ]
            tokens: dict[tuple[str, str], str | None] = {}
            for _, user_id, _ in candidates:
                for provider in readers:
                    key = (provider, str(user_id))
                    if key not in tokens:
                        tokens[key] = await reference_token(db, provider, user_id)
    except Exception as exc:
        logger.warning("Workflow run delivery on %s not read back: %s", event.repo, format_exception_message(exc))
        return counts
    seen: dict[tuple[Any, ...], dict[str, Any] | None] = {}
    for item_id, user_id, refs in candidates:
        for ref in named_by_workflow_run(refs, event):
            await _poll_workflow_run(item_id, user_id, ref, readers, tokens, seen, counts, HINT_MAX_READS)
    counts.pop("_unread", None)
    if counts["read"]:
        logger.info("Workflow run %s on %s read back for blocked items: %s", event.run_id, event.repo, counts)
    return counts


def schedule_workflow_run_release(event: PullRequestEvent) -> None:
    """Run :func:`release_from_workflow_run_delivery` in the background (a webhook must not wait on GitHub)."""
    if delivered_workflow_names(event):
        _spawn(release_from_workflow_run_delivery(event))


async def check_item_workflow_runs(item_id: uuid.UUID | str) -> dict[str, Any]:
    """Read the open ``workflow_run`` references of one item once, at the write that blocks it.

    A run that already succeeded after ``after`` sends no delivery any more: without this
    read the item waits for the next run of the workflow or the polling pass. Same reader,
    credential and apply as the polling fallback. A reference the reader cannot read (a
    workflow the code host does not know, #386) is told on the item at once, without the
    quiet time the poller waits for. ``{"read": r, "released": n, "noted": m}``.
    Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import workflow_run_readers

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    readers = workflow_run_readers()
    if not readers:
        return counts
    try:
        item_uuid = uuid.UUID(str(item_id))
        async with async_session() as db:
            row = (
                await db.execute(
                    select(WorkItem.user_id, WorkItem.status, WorkItem.blocked_on_refs).where(WorkItem.id == item_uuid)
                )
            ).one_or_none()
            if row is None or row[0] is None or row[1] != "blocked":
                return counts
            waiting = [r for r in open_refs(row[2]) if r.get("kind") == "workflow_run"]
            tokens: dict[tuple[str, str], str | None] = {}
            for provider in readers if waiting else ():
                tokens[(provider, str(row[0]))] = await reference_token(db, provider, row[0])
        seen: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        for ref in waiting[:POLL_MAX_ITEMS]:
            await _poll_workflow_run(item_uuid, row[0], ref, readers, tokens, seen, counts, HINT_MAX_READS)
    except Exception as exc:
        logger.warning("Workflow run check of work item %s failed: %s", item_id, format_exception_message(exc))
    unread: dict[uuid.UUID, list[str]] = counts.pop("_unread", {})
    if unread:
        # The write that blocked it is the moment its owner reads it: no quiet time (#386).
        await note_unread_references(unread, datetime.now(UTC), quiet=False)
    return counts


def schedule_workflow_run_check(item_id: uuid.UUID | str) -> None:
    """Run :func:`check_item_workflow_runs` in the background (a write must not wait on the code host)."""
    _spawn(check_item_workflow_runs(item_id))


# -- The tag references (#2209) ------------------------------------------------------


def tag_note(ref: dict[str, Any], tag: dict[str, Any], now: datetime) -> str:
    """The note of an item a tag carrying its commit released. Pure."""
    commit = str(tag.get("commit") or "")[:12]
    return f"released: {ref_label(ref)}: tag {tag.get('tag')} at {commit}; released at {_stamp(now)}"


def names_tag(refs: object) -> bool:
    """Whether *refs* (normalised ``blocked_on_refs``) waits for a tag. Pure."""
    items = refs if isinstance(refs, list) else []
    return any(isinstance(r, dict) and r.get("kind") == "tag" for r in items)


async def _apply_tag(item_id: uuid.UUID, key: tuple[Any, ...], tag: dict[str, Any], now: datetime) -> str | None:
    """Mark the ``tag`` reference *key* of one item created by *tag*. ``released`` / ``noted`` / ``None``."""
    from app.core.database import async_session

    async with async_session() as db:
        item = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None or item.status != "blocked" or not item.blocked_on_refs:
            return None
        refs: list[dict[str, Any]] = []
        matched: list[dict[str, Any]] = []
        for ref in item.blocked_on_refs:
            ref = dict(ref)
            if ref.get("kind") == "tag" and _ref_key(ref) == key and not _satisfied(ref):
                ref["state"] = "created"
                ref["at"] = now.isoformat()
                ref["tag"] = str(tag.get("tag") or "")
                ref["tag_commit"] = str(tag.get("commit") or "")
                matched.append(ref)
            refs.append(ref)
        if not matched:
            return None
        item.blocked_on_refs = refs
        notes = list(item.claim_notes or [])
        texts = [tag_note(r, tag, now) for r in matched]
        outcome = "noted"
        note: str | None = None
        if all_satisfied(refs) and not item.blocked_on:
            note = "\n".join(texts) + due_deferral(item.not_before, now)
            previous = item.description or ""
            item.description = f"{note}\n\n{previous}" if previous else note
            notes.append({"at": now.isoformat(), "text": note})
            item.status = "open"
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            item.blocked_on_refs = None
            item.blocked_reason = None
            item.needs_human = None
            item.last_activity_at = now
            outcome = "released"
        else:
            notes.extend({"at": now.isoformat(), "text": f"{t}; still waiting"} for t in texts)
        item.claim_notes = notes
        await db.commit()
    if outcome == "released":
        await _publish_tag(item_id, note or "", tag, matched)
    return outcome


async def _publish_tag(item_id: uuid.UUID, note: str, tag: dict[str, Any], matched: list[dict[str, Any]]) -> None:
    from app.core.database import async_session
    from app.services.platform_events import publish, work_item_data
    from app.services.work_item import publish_transition

    await publish_transition(item_id, previous_status="blocked", note=note)
    try:
        async with async_session() as db:
            item = await db.get(WorkItem, item_id)
        if item is None:
            return
        data = work_item_data(item, previous_status="blocked", note=note)
        data["released_by"] = {
            "kind": "tag",
            "tag": tag.get("tag"),
            "commit": tag.get("commit"),
            "references": [ref_label(r) for r in matched],
        }
        await publish("work_item.released", data)
    except Exception as exc:  # best-effort: the release already landed
        logger.warning("work_item.released for %s not published: %s", item_id, format_exception_message(exc))


async def _poll_tag(
    item_id: uuid.UUID,
    user_id: Any,
    ref: dict[str, Any],
    readers: dict[str, Any],
    tokens: dict[tuple[str, str], str | None],
    seen: dict[tuple[Any, ...], dict[str, Any] | None],
    counts: dict[str, Any],
    max_reads: int = POLL_MAX_READS,
) -> bool:
    """Read one ``tag`` reference through the providers' tag readers and apply a carrying tag.

    False when the read budget left the reference unread, so the item is not stamped polled.
    """
    key = _ref_key(ref)
    for provider, reader in readers.items():
        token = tokens.get((provider, str(user_id)))
        if not token:
            continue
        read_key = (provider, str(user_id), *key)
        if read_key not in seen:
            if counts["read"] >= max_reads:
                return False
            counts["read"] += 1
            try:
                seen[read_key] = await reader(str(ref["repo"]), str(ref["pattern"]), str(ref["carries_sha"]), token)
            except Exception as exc:
                _note_read_failure(counts, ref, exc, item_id)
                seen[read_key] = None
        tag = seen[read_key]
        if not tag:
            continue
        try:
            outcome = await _apply_tag(item_id, key, tag, datetime.now(UTC))
        except Exception as exc:
            logger.warning(
                "Polled reference %s could not move work item %s: %s",
                ref_label(ref),
                item_id,
                format_exception_message(exc),
            )
            return True
        if outcome:
            counts[outcome] = counts.get(outcome, 0) + 1
        return True
    return True


async def check_item_tags(item_id: uuid.UUID | str) -> dict[str, Any]:
    """Read the open ``tag`` references of one item once, at the write that blocks it.

    The tag may exist already: without this read the item waits for the polling
    pass. ``{"read": r, "released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import tag_readers

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    readers = tag_readers()
    if not readers:
        return counts
    try:
        item_uuid = uuid.UUID(str(item_id))
        async with async_session() as db:
            row = (
                await db.execute(
                    select(WorkItem.user_id, WorkItem.status, WorkItem.blocked_on_refs).where(WorkItem.id == item_uuid)
                )
            ).one_or_none()
            if row is None or row[0] is None or row[1] != "blocked":
                return counts
            waiting = [r for r in open_refs(row[2]) if r.get("kind") == "tag"]
            tokens: dict[tuple[str, str], str | None] = {}
            for provider in readers if waiting else ():
                tokens[(provider, str(row[0]))] = await reference_token(db, provider, row[0])
        seen: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        for ref in waiting[:POLL_MAX_ITEMS]:
            await _poll_tag(item_uuid, row[0], ref, readers, tokens, seen, counts, HINT_MAX_READS)
    except Exception as exc:
        logger.warning("Tag check of work item %s failed: %s", item_id, format_exception_message(exc))
    unread: dict[uuid.UUID, list[str]] = counts.pop("_unread", {})
    if unread:
        await note_unread_references(unread, datetime.now(UTC), quiet=False)
    return counts


def schedule_tag_check(item_id: uuid.UUID | str) -> None:
    """Run :func:`check_item_tags` in the background (a write must not wait on the code host)."""
    _spawn(check_item_tags(item_id))


def named_by_tag(refs: list[Any], event: PullRequestEvent) -> list[dict[str, Any]]:
    """The open ``tag`` references of *refs* a tag creation *event* may satisfy. Pure.

    Same repository and a tag name matching the reference's pattern; whether the
    tag carries the commit is the reader's answer, read back before anything applies.
    """
    import fnmatch

    name = (event.extra or {}).get("tag")
    if event.kind != "tag_created" or not isinstance(name, str) or not name:
        return []
    return [
        r
        for r in open_refs(refs)
        if r.get("kind") == "tag"
        and r.get("repo") == event.repo
        and fnmatch.fnmatchcase(name, str(r.get("pattern") or "*"))
    ]


async def release_from_tag_delivery(event: PullRequestEvent) -> dict[str, Any]:
    """Release the items waiting on a tag a tag creation delivery reports, after a read back.

    ``{"read": r, "released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import tag_readers

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    readers = tag_readers()
    if event.kind != "tag_created" or not readers:
        return counts
    try:
        async with async_session() as db:
            candidates = [c for c in await _hint_candidates(db, event, named_by_tag) if named_by_tag(c[2], event)]
            tokens: dict[tuple[str, str], str | None] = {}
            for _, user_id, _ in candidates:
                for provider in readers:
                    key = (provider, str(user_id))
                    if key not in tokens:
                        tokens[key] = await reference_token(db, provider, user_id)
    except Exception as exc:
        logger.warning("Tag delivery on %s not read back: %s", event.repo, format_exception_message(exc))
        return counts
    seen: dict[tuple[Any, ...], dict[str, Any] | None] = {}
    for item_id, user_id, refs in candidates:
        for ref in named_by_tag(refs, event):
            await _poll_tag(item_id, user_id, ref, readers, tokens, seen, counts, HINT_MAX_READS)
    counts.pop("_unread", None)
    return counts


def schedule_tag_release(event: PullRequestEvent) -> None:
    """Run :func:`release_from_tag_delivery` in the background (a webhook must not wait on the code host)."""
    if event.kind == "tag_created":
        _spawn(release_from_tag_delivery(event))


#: The reference kinds :func:`check_item_reference_states` reads at the write.
WRITE_CHECKED_KINDS = ("pull_request", "issue")


def names_reference_state(refs: object) -> bool:
    """Whether *refs* (normalised ``blocked_on_refs``) waits for a pull request or an issue. Pure."""
    items = refs if isinstance(refs, list) else []
    return any(isinstance(r, dict) and r.get("kind") in WRITE_CHECKED_KINDS for r in items)


async def check_item_reference_states(item_id: uuid.UUID | str) -> dict[str, Any]:
    """Read the open ``pull_request`` and ``issue`` references of one item once, at the write that blocks it.

    A pull request merged or an issue closed before the write sends no delivery
    any more: without this read the item waits for the polling pass (minutes).
    Same reader, credential and apply as the polling fallback; a deployment
    reference has its own check (:func:`check_item_deployment`), a workflow run
    its own (:func:`check_item_workflow_runs`). ``{"read": r, "released": n,
    "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import reference_state_readers

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    readers = reference_state_readers()
    if not readers:
        return counts
    try:
        item_uuid = uuid.UUID(str(item_id))
        async with async_session() as db:
            row = (
                await db.execute(
                    select(WorkItem.user_id, WorkItem.status, WorkItem.blocked_on_refs).where(WorkItem.id == item_uuid)
                )
            ).one_or_none()
            if row is None or row[0] is None or row[1] != "blocked":
                return counts
            waiting = [r for r in open_refs(row[2]) if r.get("kind") in WRITE_CHECKED_KINDS]
            tokens: dict[str, str | None] = {}
            for provider in readers if waiting else ():
                tokens[provider] = await reference_token(db, provider, row[0])
        for ref in waiting[:HINT_MAX_READS]:
            kind = read_kind(ref)
            if kind is None:
                continue
            for provider, reader in readers.items():
                token = tokens.get(provider)
                if not token:
                    continue
                counts["read"] += 1
                try:
                    event = await reader(str(ref["repo"]), kind, int(ref["number"]), token)
                except Exception as exc:
                    logger.info("Reference %s of work item %s not read at the write: %s", ref_label(ref), item_id, exc)
                    continue
                if event is None or event.kind == "dequeued":
                    # Still open (or out of a merge queue, which the poller reports): the
                    # deliveries and the polling pass go on as before.
                    continue
                outcome = await _apply_to_item(item_uuid, event, datetime.now(UTC))
                if outcome:
                    counts[outcome] = counts.get(outcome, 0) + 1
                break
    except Exception as exc:
        logger.warning("Reference check of work item %s failed: %s", item_id, format_exception_message(exc))
    return counts


def schedule_reference_state_check(item_id: uuid.UUID | str) -> None:
    """Run :func:`check_item_reference_states` in the background (a write must not wait on the code host)."""
    _spawn(check_item_reference_states(item_id))


# -- The polling fallback --------------------------------------------------------

#: Item id -> monotonic time of its last read, per process.
_last_polled: dict[uuid.UUID, float] = {}
# Items a pass left with a reference unread because its read budget ran out: never
# stamped polled, and read first by the next pass of this process.
_budget_skipped: set[uuid.UUID] = set()


def open_refs(refs: Any) -> list[dict]:
    """The references of *refs* not satisfied yet. Pure."""
    return [r for r in (refs or []) if isinstance(r, dict) and not _satisfied(r)]


def _due(item_id: uuid.UUID, now_mono: float) -> bool:
    last = _last_polled.get(item_id)
    return last is None or now_mono - last >= POLL_QUIET_SECONDS


async def _poll_candidates(db, now: datetime) -> list[tuple[uuid.UUID, Any, list]]:
    rows = await db.execute(
        select(WorkItem.id, WorkItem.user_id, WorkItem.blocked_on_refs)
        .where(
            WorkItem.status == "blocked",
            WorkItem.blocked_on_refs.is_not(None),
            WorkItem.action_mode.is_(None),
            WorkItem.user_id.is_not(None),
            WorkItem.last_activity_at <= now - timedelta(seconds=POLL_MIN_AGE_SECONDS),
        )
        .order_by(WorkItem.last_activity_at.asc())
        # The whole blocked set up to the scan cap: the pass keeps the items not read
        # in the quiet window (:func:`_due`), so a window of the oldest items only
        # left every newer one unread once those had all been read (an item
        # re-parked after a merge queue removal waited about 20 h for its merge).
        .limit(_CANDIDATE_SCAN)
    )
    return [(row[0], row[1], list(row[2] or [])) for row in rows.all()]


async def reference_token(db, provider: str, user_id: Any) -> str | None:
    """The token *user_id* reads *provider* with: the first of its integrations
    they see, connected for them, whose credential they may use (the event
    poller's rule); ``None`` when there is none."""
    from app.models.integration import Integration
    from app.services.integration_user_credentials import (
        _visible_to,
        connected_for_clause,
        reader_clause,
        resolve_token_holder,
    )
    from app.services.vault import reveal_field

    integrations = (
        (
            await db.execute(
                select(Integration)
                .where(
                    Integration.provider == provider,
                    connected_for_clause(user_id),
                    reader_clause(user_id),
                )
                .order_by(Integration.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    for integration in integrations:
        if not await _visible_to(db, integration.id, user_id):
            continue
        holder = await resolve_token_holder(db, integration, user_id)
        if holder is None:
            continue
        token = await reveal_field(holder, "access_token_enc")
        if not token and holder is integration:
            token = await reveal_field(integration, "api_key_enc")
        if token:
            return token
    return None


async def release_red_self_blocks(now: datetime | None = None) -> int:
    """Release the items blocked on the merge of their own pull request red for :data:`RED_SELF_BLOCK_SECONDS`.

    A red head merges only once the item's owner pushes a fix, so such a wait
    never ends by itself: the item goes back to ``open``, unclaimed, its
    references cleared, with :func:`red_self_block_note` on top of its
    description. An item that also waits for work items or for another
    reference stays blocked (those wake it). Never raises.
    """
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    released: list[tuple[uuid.UUID, str]] = []
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == "blocked",
                    WorkItem.blocked_on_refs.is_not(None),
                    WorkItem.action_mode.is_(None),
                    WorkItem.last_activity_at <= now - timedelta(seconds=POLL_MIN_AGE_SECONDS),
                    # Only items whose pull requests have a CI recorded: the rest is decided in Python.
                    cast(WorkItem.payload, Text).ilike('%"ci"%'),
                )
                .order_by(WorkItem.last_activity_at.asc())
                .limit(POLL_MAX_ITEMS * 4)
                .with_for_update(skip_locked=True)
            )
            for item in rows.scalars().all():
                if item.blocked_on:
                    continue
                stuck = _own_red_refs(list(item.blocked_on_refs or []), item.payload)
                if len(stuck) != len(open_refs(item.blocked_on_refs)):
                    continue  # it also waits for something else that may still wake it
                if not red_self_block_due(stuck, item.last_activity_at, now):
                    continue
                note = red_self_block_note(stuck, now)
                previous = item.description or ""
                item.description = f"{note}\n\n{previous}" if previous else note
                item.claim_notes = [*(item.claim_notes or []), {"at": now.isoformat(), "text": note}]
                item.status = "open"
                item.claimed_by_agent_id = None
                item.claimed_by_run_id = None
                item.claimed_at = None
                item.blocked_on_refs = None
                item.blocked_reason = None
                item.needs_human = None
                item.last_activity_at = now
                released.append((item.id, note))
            await db.commit()
    except Exception as exc:
        logger.warning("Items blocked on their own red pull request not released: %s", format_exception_message(exc))
        return 0
    for item_id, note in released:
        await _publish_surfaced(item_id, note)
    if released:
        logger.info("Released %d work item(s) blocked on their own red pull request", len(released))
    return len(released)


async def poll_blocked_on_refs(now: datetime | None = None) -> dict:
    """Read the open references of the blocked items no delivery released.

    ``{"read": r, "released": n, "noted": m}``. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import (
        branch_closings_readers,
        reference_state_readers,
        tag_readers,
        workflow_run_readers,
    )

    counts: dict[str, Any] = {"read": 0, "released": 0, "noted": 0}
    readers = reference_state_readers()
    run_readers = workflow_run_readers()
    tag_reader_map = tag_readers()
    branch_readers = branch_closings_readers()
    if not readers and not run_readers and not tag_reader_map:
        return counts
    providers = list(dict.fromkeys([*readers, *run_readers, *tag_reader_map]))
    now = now or datetime.now(UTC)
    now_mono = time.monotonic()
    try:
        async with async_session() as db:
            due = [c for c in await _poll_candidates(db, now) if _due(c[0], now_mono)]
            # An item a previous pass left unread for lack of budget goes first, so a
            # long queue of blocked items cannot starve it pass after pass.
            due.sort(key=lambda c: c[0] not in _budget_skipped)
            candidates = due[:POLL_MAX_ITEMS]
            tokens: dict[tuple[str, str], str | None] = {}
            for _, user_id, _ in candidates:
                for provider in providers:
                    key = (provider, str(user_id))
                    if key not in tokens:
                        tokens[key] = await reference_token(db, provider, user_id)
    except Exception as exc:
        logger.warning("Blocked references not polled: %s", format_exception_message(exc))
        return counts
    seen: dict[tuple, PullRequestEvent | None] = {}
    runs_seen: dict[tuple, dict | None] = {}
    tags_seen: dict[tuple[Any, ...], dict[str, Any] | None] = {}
    branches_seen: dict[tuple, list[PullRequestEvent]] = {}
    for item_id, user_id, refs in candidates:
        complete = True
        for ref in open_refs(refs):
            if ref.get("kind") == "workflow_run":
                if not await _poll_workflow_run(item_id, user_id, ref, run_readers, tokens, runs_seen, counts):
                    complete = False
                continue
            if ref.get("kind") == "tag":
                if not await _poll_tag(item_id, user_id, ref, tag_reader_map, tokens, tags_seen, counts):
                    complete = False
                continue
            kind = read_kind(ref)
            if kind is None:
                continue
            for provider, reader in readers.items():
                token = tokens.get((provider, str(user_id)))
                if not token:
                    continue
                key = (provider, str(user_id), ref.get("repo"), kind, ref.get("number"))
                if key not in seen:
                    if counts["read"] >= POLL_MAX_READS:
                        complete = False
                        break
                    counts["read"] += 1
                    try:
                        seen[key] = await reader(str(ref["repo"]), kind, int(ref["number"]), token)
                    except Exception as exc:
                        _note_read_failure(counts, ref, exc, item_id)
                        seen[key] = None
                event = seen[key]
                if event is None:
                    continue
                try:
                    if event.kind == "dequeued":
                        # A merge queue removal no delivery reported: the owner is told (#606).
                        outcome = await _apply_dequeue_to_item(item_id, event, datetime.now(UTC), polled=True)
                    else:
                        outcome = await _apply_to_item(item_id, event, datetime.now(UTC))
                except Exception as exc:
                    logger.warning(
                        "Polled reference %s could not move work item %s: %s",
                        ref_label(ref),
                        item_id,
                        format_exception_message(exc),
                    )
                    continue
                if outcome:
                    counts[outcome] = counts.get(outcome, 0) + 1
                break
            if ref.get("kind") == "pull_request" and ref.get("head_branch"):
                await _poll_branch_successors(item_id, user_id, ref, branch_readers, tokens, branches_seen, counts)
        if complete:
            _last_polled[item_id] = now_mono
            _budget_skipped.discard(item_id)
        else:
            counts["skipped_by_budget"] = counts.get("skipped_by_budget", 0) + 1
            _budget_skipped.add(item_id)
    if len(_last_polled) > POLL_MAX_ITEMS * 40:
        for stale in [k for k, t in _last_polled.items() if now_mono - t >= POLL_QUIET_SECONDS]:
            _last_polled.pop(stale, None)
    unread: dict[uuid.UUID, list[str]] = counts.pop("_unread", {})
    if unread:
        noted = await note_unread_references(unread, now)
        if noted:
            counts["failure_noted"] = noted
    if counts.get("skipped_by_budget") or counts.get("read_failed"):
        logger.warning("Blocked references polled with items left unread: %s", counts)
    elif counts["released"] or counts["noted"] or counts.get("surfaced"):
        logger.info("Blocked references polled: %s", counts)
    return counts


def _note_read_failure(
    counts: dict[str, Any], ref: dict[str, Any], exc: Exception, item_id: uuid.UUID | None = None
) -> None:
    """Count and log, at warning, a reference the provider could not read, kept per item for its note."""
    message = format_exception_message(exc)
    counts["read_failed"] = counts.get("read_failed", 0) + 1
    logger.warning("Reference %s not read: %s", ref_label(ref), message)
    if item_id is not None:
        counts.setdefault("_unread", {}).setdefault(item_id, []).append(f"{ref_label(ref)}: {message}")


# -- What the item says when a reference cannot be read -----------------------------

#: An item still blocked this long after it was blocked is told why a read failed.
READ_FAILURE_NOTE_AFTER_SECONDS = 15 * 60
#: One read-failure note per item at most this often (the poller retries every pass).
READ_FAILURE_NOTE_EVERY_SECONDS = 6 * 3600
READ_FAILURE_NOTE_PREFIX = "not released yet: a reference could not be read"


def read_failure_note(failures: list[str], now: datetime) -> str:
    """The claim note of an item whose references the poller could not read. Pure."""
    reasons = "; ".join(dict.fromkeys(failures))[:MAX_REASON_CHARS]
    every = POLL_QUIET_SECONDS // 60
    return f"{READ_FAILURE_NOTE_PREFIX} at {_stamp(now)} ({reasons}); the poller reads it again every {every} min"


def read_failure_note_due(claim_notes: Any, last_activity_at: datetime | None, now: datetime) -> bool:
    """Whether an item may get a read-failure note now. Pure.

    Blocked and quiet for :data:`READ_FAILURE_NOTE_AFTER_SECONDS`, and no such note
    in the last :data:`READ_FAILURE_NOTE_EVERY_SECONDS`.
    """
    if last_activity_at is not None and now - last_activity_at < timedelta(seconds=READ_FAILURE_NOTE_AFTER_SECONDS):
        return False
    horizon = now - timedelta(seconds=READ_FAILURE_NOTE_EVERY_SECONDS)
    for note in claim_notes or []:
        if not isinstance(note, dict) or not str(note.get("text") or "").startswith(READ_FAILURE_NOTE_PREFIX):
            continue
        at = _parse_stamp(note.get("at"))
        if at is not None and at >= horizon:
            return False
    return True


async def note_unread_references(unread: dict[uuid.UUID, list[str]], now: datetime, *, quiet: bool = True) -> int:
    """Write :func:`read_failure_note` on the blocked items of *unread* it is due on. Never raises.

    The note goes in the claim notes, the item's activity time untouched, so the
    polling order is unchanged. *quiet* False (the check at the write that blocks the
    item) skips the quiet time, never the one-note-per-6-hours rule.
    """
    from app.core.database import async_session

    noted = 0
    try:
        async with async_session() as db:
            for item_id, failures in unread.items():
                item = (
                    await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
                ).scalar_one_or_none()
                if item is None or item.status != "blocked":
                    continue
                if not read_failure_note_due(item.claim_notes, item.last_activity_at if quiet else None, now):
                    continue
                item.claim_notes = [
                    *(item.claim_notes or []),
                    {"at": now.isoformat(), "text": read_failure_note(failures, now)},
                ]
                noted += 1
            await db.commit()
    except Exception as exc:
        logger.warning("Read failures not noted on their items: %s", format_exception_message(exc))
    return noted


# -- The backstop: every reference met, the item still blocked ----------------------


def backstop_note(refs: list[dict[str, Any]], now: datetime) -> str:
    """The note of an item the backstop released. Pure."""
    met = "; ".join(ref_label(r) for r in refs)
    return f"released by the backstop: every reference was already met ({met}); released at {_stamp(now)}"


async def release_satisfied_blocks(now: datetime | None = None) -> int:
    """Release the blocked items whose references are all met but that stayed blocked.

    The class of defect, whatever path missed the release (a delivery lost, a
    release that failed after recording the state): an item blocked on
    references only, every one satisfied, no work item and no person to wait
    for, goes back to ``open``, unclaimed, with :func:`backstop_note` on top of
    its description. Run once per polling pass. Never raises.
    """
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    released: list[tuple[uuid.UUID, str]] = []
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(WorkItem)
                .where(
                    WorkItem.status == "blocked",
                    WorkItem.blocked_on_refs.is_not(None),
                    WorkItem.action_mode.is_(None),
                    WorkItem.last_activity_at <= now - timedelta(seconds=POLL_MIN_AGE_SECONDS),
                    # Only items with a recorded state: an unmet reference carries none.
                    cast(WorkItem.blocked_on_refs, Text).ilike('%"state"%'),
                )
                .order_by(WorkItem.last_activity_at.asc())
                .limit(POLL_MAX_ITEMS * 4)
                .with_for_update(skip_locked=True)
            )
            for item in rows.scalars().all():
                refs = [r for r in (item.blocked_on_refs or []) if isinstance(r, dict)]
                if item.blocked_on or item.needs_human or not all_satisfied(refs):
                    continue
                note = backstop_note(refs, now) + due_deferral(item.not_before, now)
                previous = item.description or ""
                item.description = f"{note}\n\n{previous}" if previous else note
                item.claim_notes = [*(item.claim_notes or []), {"at": now.isoformat(), "text": note}]
                item.status = "open"
                item.claimed_by_agent_id = None
                item.claimed_by_run_id = None
                item.claimed_at = None
                item.blocked_on_refs = None
                item.blocked_reason = None
                item.needs_human = None
                item.last_activity_at = now
                released.append((item.id, note))
            await db.commit()
    except Exception as exc:
        logger.warning("Blocked items with every reference met not released: %s", format_exception_message(exc))
        return 0
    for item_id, note in released:
        await _publish_surfaced(item_id, note)
    if released:
        logger.warning("Backstop released %d blocked work item(s) whose references were all met", len(released))
    return len(released)
