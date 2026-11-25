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

"""A work item waits on its pull requests and wakes up when their CI ends.

A work item may name the pull requests it waits on, in its payload::

    payload.pull_requests = [
        {"repo": "owner/name", "number": 12, "head_branch": "feat/x",
         "head_sha": "...", "url": "https://...", "state": "open", "draft": false,
         "ci": {"head_sha": "...", "runs": {<run key>: {...}}, "dispatched": [...]}},
    ]
    payload.complete_on_merge = true   # optional

Written by :func:`record_pull_request` (the code host's create-PR tool names
the item it opens the pull request for) or by any payload write. A provider
turns its webhook deliveries into
:class:`~app.integrations.pull_request_events.PullRequestEvent` (the provider
seam); :func:`handle_pull_request_event` applies one to every unfinished item
that names the pull request:

* **CI** - every CI run of the bound head commit is tracked under ``ci.runs``
  (one entry per workflow or check suite, a re-run replacing the run it
  repeats). Once every known run has completed (or one failed), the item is
  dispatched at once to its owner's ``work_item`` inbox, through the same path
  as the scheduler's pickup, with ``trigger_context.reason = "pr_ci_completed"``,
  the conclusion and the run url. An item a live run holds gets a dated claim
  note instead: that run is the one working on it. So does an item blocked on
  other items: its blockers, not the CI, put it back in the queue. An item
  whose pull request a live run holds a lease on
  (``dev:branch-lock:<owner/name>:<branch>``, old form ``dev:branch-lock:<branch>``,
  ``shared_state_pull_requests.live_pull_request_lease``) starts no run either:
  that run is pushing the fix, and the inbox pass takes the item once the
  lease is released (#11480).
  One dispatch per (item, head commit, conclusion, set of CI runs): a delivery GitHub repeats, or a
  second completion of the same set, dispatches nothing; a run that appears
  later re-arms it.
* **New head** - a push moves ``head_sha`` and forgets the CI of the old head;
  ``draft`` follows the delivery (a draft is not yet waiting on its merge, so
  the inbox finalizer never parks an item on it, see ``settle_inbox_work_item``).
* **Dequeued** - the code host dropped the pull request from its merge queue
  without merging it (failed queue checks): no CI event follows, so the item
  is dispatched once per removal with the conclusion ``dequeued`` (#200).
* **Conflict** - a conflict with the base sends no event either: the CI poll
  reads it and dispatches the item once per head with ``conflict`` (#200).
* **Closed** - a dated note; with ``complete_on_merge`` the item is completed
  with the merge SHA once every pull request it names is merged. An item
  blocked on its pull requests alone (a reason, no ``blocked_on``, no open
  ``blocked_on_refs``, no person) goes back to ``open`` once none of them is
  open any more: their CI was its only waker (#10792).

Only deliveries the receiver verified with a signature reach this module
(``api/v1/webhooks.py``). Everything here is best-effort: a failure is logged
and never fails the webhook it came with. A system write, like the claim
release sweeps: it runs with no writer, the item's own owner acting on it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, and_, cast, or_, select

from app.core.utils import format_exception_message, safe_uuid
from app.integrations.pull_request_events import PullRequestEvent
from app.models.work_item import WorkItem

logger = logging.getLogger(__name__)

#: ``trigger_context.reason`` of a dispatch made because CI completed.
CI_COMPLETED_REASON = "pr_ci_completed"
#: Pull requests one item may name.
MAX_PULL_REQUESTS = 10
#: Items one delivery may move (a pull request is normally named by one).
_MAX_ITEMS_PER_EVENT = 25
#: Conclusions that do not fail a head commit's CI.
_PASSING = frozenset({"success", "neutral", "skipped"})
#: Item statuses a delivery may move.
_UNFINISHED = ("open", "claimed", "in_progress", "blocked")
_HELD = ("claimed", "in_progress")
#: Dispatch keys kept per pull request (the newest ones).
_MAX_DISPATCH_KEYS = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note(now: datetime, text: str) -> dict:
    return {"at": now.isoformat(), "text": text}


# -- The binding -----------------------------------------------------------------


def normalize_binding(entry: object) -> dict | None:
    """*entry* as a binding ``{repo, number, ...}``, ``None`` when it names none."""
    if not isinstance(entry, dict):
        return None
    repo = entry.get("repo")
    number = entry.get("number")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(repo, str) or repo.count("/") != 1 or not isinstance(number, int) or number <= 0:
        return None
    out = dict(entry)
    out["repo"] = repo.strip().lower()
    out["number"] = number
    return out


def pull_request_bindings(payload: object) -> list[dict]:
    """The pull requests a work item's *payload* names, normalised."""
    entries = payload.get("pull_requests") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [b for b in (normalize_binding(e) for e in entries) if b is not None]


def bind_pull_request(
    payload: dict | None,
    *,
    repo: str,
    number: int,
    head_branch: str | None = None,
    head_sha: str | None = None,
    url: str | None = None,
    draft: bool | None = None,
) -> dict:
    """A copy of *payload* naming the pull request (updated when already named).

    *draft* records whether it is a draft; ``None`` keeps what the binding says.

    At the cap of :data:`MAX_PULL_REQUESTS`, a merged or closed pull request
    makes room for the new one (the one closed first leaves first, see
    :func:`_evict_finished`): a long program item keeps naming the pull
    request it waits on after ten merged slices.

    Raises :class:`ValueError` for a pull request that is not one, or when the
    item already names :data:`MAX_PULL_REQUESTS` open others.
    """
    binding = normalize_binding({"repo": repo, "number": number})
    if binding is None:
        raise ValueError("a pull request is named by its repository (owner/name) and number")
    out = dict(payload or {})
    bindings = pull_request_bindings(out)
    current = next(
        (b for b in bindings if (b["repo"], b["number"]) == (binding["repo"], binding["number"])),
        None,
    )
    if current is None:
        if len(bindings) >= MAX_PULL_REQUESTS:
            bindings = _evict_finished(bindings, len(bindings) - MAX_PULL_REQUESTS + 1)
        if len(bindings) >= MAX_PULL_REQUESTS:
            raise ValueError(f"a work item names at most {MAX_PULL_REQUESTS} open pull requests")
        current = {**binding, "state": "open"}
        bindings.append(current)
    for key, value in (("head_branch", head_branch), ("head_sha", head_sha), ("url", url)):
        if value:
            current[key] = value
    if isinstance(draft, bool):
        current["draft"] = draft
    out["pull_requests"] = bindings
    return out


def is_draft(binding: dict) -> bool:
    """Whether the pull request of *binding* is known to be a draft."""
    return binding.get("draft") is True


def ci_pending(binding: dict) -> bool:
    """Whether the CI of *binding*'s current head has not completed (none recorded: pending)."""
    ci = binding.get("ci") if isinstance(binding.get("ci"), dict) else {}
    head = binding.get("head_sha")
    if head and ci.get("head_sha") and ci.get("head_sha") != head:
        return True  # the CI recorded is an older head's
    return _ci_conclusion(ci) is None


def _evict_finished(bindings: list[dict], count: int) -> list[dict]:
    """*bindings* without up to *count* merged or closed ones, the oldest first. Pure.

    Oldest is the earliest ``closed_at``; a binding with none (written before
    the stamp, or by hand) counts as older than any stamped one, and ties keep
    payload order. Open bindings are never evicted; the rest keep their order.
    """
    finished = [i for i, b in enumerate(bindings) if b.get("state", "open") != "open"]
    finished.sort(key=lambda i: (bindings[i].get("closed_at") is not None, str(bindings[i].get("closed_at") or ""), i))
    gone = set(finished[: max(count, 0)])
    return [b for i, b in enumerate(bindings) if i not in gone]


def pull_requests_waiting_for_ci(item: dict) -> list[dict]:
    """Every open pull request of *item* whose CI has not completed, in payload order.

    What the views say an open or blocked item waits for: its CI, not a pickup.
    A pull request with no CI recorded yet waits for its first run.
    """
    waiting: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for binding in pull_request_bindings(item.get("payload")):
        # The first entry naming a pull request wins, as in the views' chips:
        # a later duplicate never reopens one listed closed or merged.
        key = (binding["repo"], binding["number"])
        if key in seen:
            continue
        seen.add(key)
        if binding.get("state", "open") != "open":
            continue
        ci = binding.get("ci") if isinstance(binding.get("ci"), dict) else {}
        if _ci_conclusion(ci) is None:
            waiting.append(
                {
                    "repo": binding["repo"],
                    "number": binding["number"],
                    "url": binding.get("url"),
                }
            )
    return waiting


def red_pull_requests(payload: object) -> list[dict]:
    """The open pull requests of *payload* whose CI on the current head failed. Pure.

    Each is the binding with ``failed_at`` (when the platform first saw the head
    fail, ``None`` for a failure recorded before that stamp existed). A red pull
    request cannot merge until its owner pushes a fix: an item must never wait
    on its merge (``work_item_blocked_refs.refuse_blocked_on_red_pull_request``).
    """
    red: list[dict] = []
    for binding in pull_request_bindings(payload):
        if binding.get("state", "open") != "open":
            continue
        ci = binding.get("ci") if isinstance(binding.get("ci"), dict) else {}
        head = binding.get("head_sha")
        if head and ci.get("head_sha") and ci.get("head_sha") != head:
            continue  # the CI recorded is an older head's
        if _ci_conclusion(ci) == "failure":
            red.append({**binding, "failed_at": ci.get("failed_at")})
    return red


async def read_pull_request_closings(bindings: list[dict], user_id: Any) -> list[PullRequestEvent]:
    """The closings of *bindings* read live from their code host with *user_id*'s credential.

    One read per pull request, through the reader the polling fallback of
    ``blocked_on_refs`` uses (``integrations/reference_state``): a closed or
    merged pull request answers its ``closed`` event, an open or unreadable one
    answers nothing. Never raises (#9507).
    """
    from app.core.database import async_session
    from app.integrations.reference_state import reference_state_readers
    from app.services.work_item_blocked_refs import reference_token

    readers = reference_state_readers()
    if not readers or not bindings:
        return []
    try:
        async with async_session() as db:
            tokens = {provider: await reference_token(db, provider, user_id) for provider in readers}
    except Exception as exc:
        logger.info("Pull request states not read: %s", format_exception_message(exc))
        return []
    out: list[PullRequestEvent] = []
    seen: set[tuple[str, int]] = set()
    for binding in bindings[:MAX_PULL_REQUESTS]:
        key = (binding["repo"], binding["number"])
        if key in seen:
            continue
        seen.add(key)
        for provider, reader in readers.items():
            token = tokens.get(provider)
            if not token:
                continue
            try:
                event = await reader(binding["repo"], "pull_request", binding["number"], token)
            except Exception as exc:
                logger.info("Pull request %s#%s not read: %s", *key, format_exception_message(exc))
                event = None
            if event is not None and event.kind == "closed":
                # A merge queue removal of an open pull request is not a closing (#606).
                out.append(event)
                break
            if event is not None:
                break
    return out


async def read_live_ci(bindings: list[dict], user_id: Any) -> dict[tuple[str, int], dict]:
    """The live head and CI verdict of each of *bindings*, by ``(repo, number)`` (#10751).

    One read per pull request through the provider's
    :data:`~app.integrations.reference_state.PullRequestCiReader` with *user_id*'s
    credential, as the polling fallback reads them. A closed or unreadable pull
    request is left out. Never raises.
    """
    from app.core.database import async_session
    from app.integrations.reference_state import pull_request_ci_readers
    from app.services.work_item_blocked_refs import reference_token

    readers = pull_request_ci_readers()
    if not readers or not bindings or user_id is None:
        return {}
    try:
        async with async_session() as db:
            tokens = {provider: await reference_token(db, provider, user_id) for provider in readers}
    except Exception as exc:
        logger.info("Pull request CI not read: %s", format_exception_message(exc))
        return {}
    out: dict[tuple[str, int], dict] = {}
    for binding in bindings[:MAX_PULL_REQUESTS]:
        key = (binding["repo"], binding["number"])
        if key in out:
            continue
        for provider, reader in readers.items():
            token = tokens.get(provider)
            if not token:
                continue
            try:
                state = await reader(binding["repo"], binding["number"], token)
            except Exception as exc:
                logger.info("Pull request %s#%s CI not read: %s", *key, format_exception_message(exc))
                state = None
            if isinstance(state, dict):
                out[key] = state
                break
    return out


def void_stale_reds(payload: object, reads: dict[tuple[str, int], dict]) -> tuple[dict, list[str]]:
    """*payload* with every recorded red the live *reads* contradict voided, and what was voided. Pure.

    A red binding whose pull request's live head differs from the head it
    recorded moved past that red (an update-branch merge, a push whose delivery
    has not come): the binding takes the live head and forgets the old CI, the
    live verdict recorded when already decided. A red binding on the same head
    whose live verdict is undecided has its failing check running again: the
    failing runs are recorded in progress. A red binding on the same head whose
    live verdict, decided on the checks the base branch requires, is
    ``success`` takes that verdict: its red was on a check the base does not
    require (#11766). Otherwise the red stands (#10751).
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    bindings = pull_request_bindings(out)
    red = {(b["repo"], b["number"]) for b in red_pull_requests(out)}
    voided: list[str] = []
    for binding in bindings:
        key = (binding["repo"], binding["number"])
        state = reads.get(key)
        if key not in red or not isinstance(state, dict):
            continue
        sha = state.get("head_sha")
        if not isinstance(sha, str) or not sha:
            continue
        ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
        recorded = ci.get("head_sha") or binding.get("head_sha")
        conclusion = state.get("conclusion")
        label = f"{binding['repo']}#{binding['number']}"
        if recorded and sha != recorded:
            binding["head_sha"] = sha
            ci = {"head_sha": sha}
            if conclusion in ("success", "failure"):
                ci["runs"] = {
                    POLLED_RUN_KEY: {
                        "run_id": None,
                        "name": "checks of the head (read)",
                        "status": "completed",
                        "conclusion": conclusion,
                        "url": state.get("run_url"),
                    }
                }
            voided.append(f"{label} moved from {str(recorded)[:12]} to {sha[:12]}")
        elif conclusion is None:
            runs = dict(ci.get("runs") or {}) if isinstance(ci.get("runs"), dict) else {}
            for name, run in list(runs.items()):
                if (
                    isinstance(run, dict)
                    and run.get("status") == "completed"
                    and (run.get("conclusion") or "") not in _PASSING
                ):
                    runs[name] = {**run, "status": "in_progress", "conclusion": None}
            ci["runs"] = runs
            ci.pop("failed_at", None)
            voided.append(f"{label} has its failing check running again on {sha[:12]}")
        elif conclusion == "success":
            ci["runs"] = {
                POLLED_RUN_KEY: {
                    "run_id": None,
                    "name": "checks of the head (read)",
                    "status": "completed",
                    "conclusion": "success",
                    "url": state.get("run_url"),
                }
            }
            ci.pop("failed_at", None)
            voided.append(f"{label} is green on the checks its base branch requires on {sha[:12]}")
        else:
            continue
        if isinstance(state.get("head_branch"), str) and state["head_branch"]:
            binding["head_branch"] = state["head_branch"]
        if isinstance(state.get("draft"), bool):
            binding["draft"] = state["draft"]
        binding["ci"] = ci
    if voided:
        out["pull_requests"] = bindings
    return out, voided


def refresh_open_from_live(
    payload: object, reads: dict[tuple[str, int], dict[str, Any]]
) -> tuple[dict[str, Any], list[str], set[tuple[str, int]]]:
    """*payload* with every open binding brought to its live *reads*, what changed, and the newly decided. Pure.

    What an inbox run's end decides on is read live, never the last delivery
    (#11730): the draft flag (a pull request marked ready mid-run), the head (a
    push or an update-branch merge whose delivery has not come) and the CI
    verdict of that head (a completion recorded nowhere yet). A new head
    forgets the old head's CI. A decided live verdict that differs from the one
    recorded on the same head is recorded as the head's CI; the dispatch keys
    already spent are kept and the verdict read is recorded as dispatched, so
    the polling fallback never wakes the item a second time on it. A binding
    with no read is left as it is. The third value names the bindings whose
    verdict this read decided (none recorded before on that head).
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    bindings = pull_request_bindings(out)
    changed: list[str] = []
    decided: set[tuple[str, int]] = set()
    for binding in bindings:
        if binding.get("state", "open") != "open":
            continue
        state = reads.get((binding["repo"], binding["number"]))
        if not isinstance(state, dict):
            continue
        label = f"{binding['repo']}#{binding['number']}"
        live_draft = state.get("draft")
        if isinstance(live_draft, bool) and binding.get("draft") is not live_draft:
            if "draft" in binding or live_draft:
                changed.append(f"{label} is {'a draft' if live_draft else 'ready for review'}")
            binding["draft"] = live_draft
        if isinstance(state.get("head_branch"), str) and state["head_branch"]:
            binding["head_branch"] = state["head_branch"]
        sha = state.get("head_sha")
        if not isinstance(sha, str) or not sha:
            continue
        ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
        recorded = ci.get("head_sha") or binding.get("head_sha")
        if recorded and recorded != sha:
            ci = {"head_sha": sha}
            changed.append(f"{label} moved from {str(recorded)[:12]} to {sha[:12]}")
        binding["head_sha"] = sha
        ci["head_sha"] = sha
        conclusion = state.get("conclusion")
        if conclusion in ("success", "failure") and _ci_conclusion(ci) != conclusion:
            ci["runs"] = {
                POLLED_RUN_KEY: {
                    "run_id": None,
                    "name": "checks of the head (read)",
                    "status": "completed",
                    "conclusion": conclusion,
                    "url": state.get("run_url"),
                }
            }
            if conclusion == "failure":
                ci.setdefault("failed_at", _now().isoformat())
            else:
                ci.pop("failed_at", None)
            key = f"{sha}:{conclusion}:{POLLED_RUN_KEY}"
            dispatched = list(ci.get("dispatched") or [])
            if key not in dispatched:
                ci["dispatched"] = [*dispatched, key][-_MAX_DISPATCH_KEYS:]
            decided.add((binding["repo"], binding["number"]))
            changed.append(f"{label} CI on {sha[:12]} already {conclusion}")
        binding["ci"] = ci
    if bindings:
        out["pull_requests"] = bindings
    return out, changed, decided


def ci_told_holder(binding: dict[str, Any]) -> bool:
    """Whether the CI completion of *binding*'s current head was told to the run holding its item. Pure.

    Such a completion was never dispatched (the holder was told instead): an
    item parked on it after that run ends would wait for a wake already spent.
    """
    raw = binding.get("ci")
    ci: dict[str, Any] = raw if isinstance(raw, dict) else {}
    head = binding.get("head_sha")
    told = ci.get("told_holder")
    return bool(told) and (not head or not ci.get("head_sha") or ci.get("head_sha") == head)


def consume_told_holder(payload: object) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """*payload* with every told-holder mark cleared, and the open bindings that carried one. Pure.

    The run's end acts once on a completion its run was told of; a later run
    end on the same head parks as before (#11730).
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    bindings = pull_request_bindings(out)
    told: list[dict[str, Any]] = []
    for binding in bindings:
        ci = binding.get("ci") if isinstance(binding.get("ci"), dict) else None
        if ci is None or "told_holder" not in ci:
            continue
        if binding.get("state", "open") == "open" and ci_told_holder(binding):
            told.append(dict(binding))
        binding["ci"] = {k: v for k, v in ci.items() if k != "told_holder"}
    if bindings:
        out["pull_requests"] = bindings
    return out, told


def _mark_told_holder(payload: object, done: CiCompleted) -> dict[str, Any]:
    """*payload* with the binding of *done* marked: its completion was told to the holding run. Pure."""
    out = dict(payload) if isinstance(payload, dict) else {}
    bindings = pull_request_bindings(out)
    for binding in bindings:
        if (binding["repo"], binding["number"]) != (done.repo, done.number):
            continue
        ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
        if done.head_sha and ci.get("head_sha") not in (None, done.head_sha):
            continue
        ci["told_holder"] = done.conclusion
        binding["ci"] = ci
    out["pull_requests"] = bindings
    return out


def waiting_for_ci(item: dict) -> dict | None:
    """The first open pull request of *item* whose CI has not completed, or ``None``."""
    waiting = pull_requests_waiting_for_ci(item)
    return waiting[0] if waiting else None


# -- Applying an event (pure) ------------------------------------------------------


@dataclass(frozen=True)
class CiCompleted:
    """The CI of a bound head commit completed: what a dispatch carries."""

    repo: str
    number: int
    head_sha: str | None
    conclusion: str
    run_url: str | None
    run_name: str | None
    dispatch_key: str


@dataclass(frozen=True)
class PullRequestClosed:
    repo: str
    number: int
    merged: bool
    merge_sha: str | None
    url: str | None
    #: Every pull request the item names is merged (``complete_on_merge``).
    all_merged: bool


def _matches(binding: dict, event: PullRequestEvent) -> bool:
    if binding["repo"] != event.repo:
        return False
    if binding["number"] in event.pr_numbers:
        return True
    # A run on a branch with no pull request listed (a fork, or GitHub not
    # attaching it): the branch is the link.
    return bool(event.head_branch) and binding.get("head_branch") == event.head_branch


def _ci_conclusion(ci: dict) -> str | None:
    """``success`` / ``failure`` once the head's CI decided, ``None`` while it runs.

    An advisory run (a ``pull_request_target`` run: the base's workflow, which
    GitHub starts before the pull request's own CI exists) and a skipped or
    neutral one (an app that skips a draft) fail a head but never make it green
    alone: success needs at least one run of the head's own CI that succeeded.
    """
    runs = ci.get("runs") if isinstance(ci.get("runs"), dict) else {}
    if not runs:
        return None
    # A run is concluded once it says completed AND names its conclusion: a
    # completed delivery with no conclusion yet is still running (#11486).
    done = [r for r in runs.values() if _concluded(r)]
    settled = len(done) == len(runs)
    # A red the live read placed outside the base branch's required checks never fails the head (#11807);
    # a provisional one (a coverage grade read before its uploads) fails it once every run concluded (#12050).
    if any(
        (r.get("conclusion") or "") not in _PASSING
        and not r.get("not_required")
        and (settled or not r.get("provisional"))
        for r in done
    ):
        return "failure"
    if not any(not r.get("advisory") and r.get("conclusion") == "success" for r in done):
        return None
    return "success" if settled else None


def _concluded(run: Any) -> bool:
    """Whether a recorded run completed with a conclusion. Pure."""
    return isinstance(run, dict) and run.get("status") == "completed" and bool(run.get("conclusion"))


def _run_identity(key: str, run: dict) -> str:
    """``<run key>#<run id>.<attempt>``: one concluded run, a re-run (new attempt) another. Pure."""
    return f"{key}#{run.get('run_id') or '?'}.{run.get('attempt') or 1}"


def ci_dispatch_key(ci: dict, conclusion: str) -> str:
    """The idempotency key of a decided CI verdict of a head. Pure.

    A failure wakes the owner once per (head, re-run attempts) (#11486): the
    workflows of the head are not in its key, so a second workflow requested,
    cancelled or failing later on a head already red builds the same key and is
    dropped; a re-run of a run (same run id, a new attempt) that concludes is a
    new key, so a re-run that ends red again wakes the owner as one that ends
    green does. A success keeps the set of runs it was decided on: a head
    called green before a later workflow was requested is green again, once,
    when that workflow concludes too.
    """
    runs = ci.get("runs") if isinstance(ci.get("runs"), dict) else {}
    sha = ci.get("head_sha") or "?"
    reruns = sorted(_run_identity(k, r) for k, r in runs.items() if _concluded(r) and int(r.get("attempt") or 1) > 1)
    if conclusion == "success":
        return f"{sha}:success:{','.join(sorted(runs))}" + (f"|{','.join(reruns)}" if reruns else "")
    return f"{sha}:{conclusion}:" + (f"|{','.join(reruns)}" if reruns else "")


def _apply_ci(binding: dict, event: PullRequestEvent) -> CiCompleted | None:
    ci = dict(binding.get("ci") or {})
    sha = event.head_sha
    current = ci.get("head_sha") or binding.get("head_sha")
    if sha and current and sha != current:
        if event.status == "completed":
            return None  # a late delivery for a head the pull request moved past
        ci = {}  # a run started on a new head: the old head's CI no longer counts
        binding["head_sha"] = sha
    if sha:
        ci["head_sha"] = sha
        binding.setdefault("head_sha", sha)
    runs = dict(ci.get("runs") or {})
    key = event.run_key or f"run:{event.run_id}"
    known = runs.get(key) if isinstance(runs.get(key), dict) else None
    if known and event.run_id is not None and (known.get("run_id") or 0) > event.run_id:
        return None  # an older run of the same source: the newer one decides
    if (
        known
        and event.run_id is not None
        and known.get("run_id") == event.run_id
        and event.run_attempt is not None
        and (known.get("attempt") or 0) > event.run_attempt
    ):
        return None  # a late delivery of an older attempt of the same run
    runs[key] = {
        "run_id": event.run_id,
        "name": event.run_name,
        "status": event.status or "queued",
        "conclusion": event.conclusion if event.status == "completed" else None,
        "url": event.run_url,
        **({"attempt": event.run_attempt} if event.run_attempt else {}),
        **({"advisory": True} if event.advisory else {}),
    }
    ci["runs"] = runs
    binding["ci"] = ci
    conclusion = _ci_conclusion(ci)
    if conclusion is None:
        return None
    dispatch_key = ci_dispatch_key(ci, conclusion)
    dispatched = list(ci.get("dispatched") or [])
    if dispatch_key in dispatched:
        # The same event again (a redelivery, another workflow of the same
        # head): dropped and counted, never a second wake (#11486).
        ci["dropped"] = int(ci.get("dropped") or 0) + 1
        logger.info(
            "CI completion %s of %s#%s already dispatched: dropped", dispatch_key, binding["repo"], binding["number"]
        )
        return None
    ci["dispatched"] = [*dispatched, dispatch_key][-_MAX_DISPATCH_KEYS:]
    reds = [r for r in runs.values() if _concluded(r) and (r.get("conclusion") or "") not in _PASSING]
    # The red that decides is one the base requires, when there is one: an unrequired red never names the dispatch.
    failed = next((r for r in reds if not r.get("not_required")), reds[0] if reds else None)
    deciding = failed or (runs.get(key) or {})
    return CiCompleted(
        repo=binding["repo"],
        number=binding["number"],
        head_sha=ci.get("head_sha"),
        conclusion=conclusion,
        run_url=deciding.get("url"),
        run_name=deciding.get("name"),
        dispatch_key=dispatch_key,
    )


#: ``conclusion`` of a dispatch made because the merge queue dropped the pull request.
DEQUEUED_CONCLUSION = "dequeued"
#: ``conclusion`` of a dispatch made because the pull request conflicts with its base.
CONFLICT_CONCLUSION = "conflict"


def _apply_dequeued(binding: dict[str, Any], event: PullRequestEvent) -> CiCompleted | None:
    """The dispatch a merge queue removal of an open pull request owes, once per removal. Pure.

    GitHub sends no CI event when it drops a pull request from its merge queue
    (failed queue checks, a conflict in the group): the item waiting on the
    merge would wait forever. The removal time keys the dispatch, so a
    redelivery is dropped and a second removal of the same head wakes again.
    """
    if binding.get("state", "open") != "open":
        return None
    ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
    sha = event.head_sha or ci.get("head_sha") or binding.get("head_sha")
    key = f"{sha or '?'}:{DEQUEUED_CONCLUSION}:{event.closed_at or event.conclusion or ''}"
    dispatched = list(ci.get("dispatched") or [])
    if key in dispatched:
        ci["dropped"] = int(ci.get("dropped") or 0) + 1
        binding["ci"] = ci
        return None
    ci["dispatched"] = [*dispatched, key][-_MAX_DISPATCH_KEYS:]
    binding["ci"] = ci
    return CiCompleted(
        repo=binding["repo"],
        number=binding["number"],
        head_sha=sha,
        conclusion=DEQUEUED_CONCLUSION,
        run_url=event.url or binding.get("url"),
        run_name=f"merge queue: removed ({event.conclusion or 'unknown'})",
        dispatch_key=key,
    )


def apply_pull_request_event(
    payload: dict | None, event: PullRequestEvent, now: datetime | None = None
) -> tuple[dict, list[CiCompleted | PullRequestClosed]]:
    """*payload* after *event*, and what it means for the item.

    Pure: the caller writes the payload back and acts on the outcomes.
    """
    now = now or _now()
    out = dict(payload or {})
    bindings = pull_request_bindings(out)
    outcomes: list[CiCompleted | PullRequestClosed] = []
    touched = False
    newly_closed = False
    for binding in bindings:
        if not _matches(binding, event):
            continue
        touched = True
        if event.kind == "ci":
            if binding.get("state", "open") != "open":
                continue
            done = _apply_ci(binding, event)
            if done is not None:
                outcomes.append(done)
            ci = binding.get("ci")
            if isinstance(ci, dict):
                ci = binding["ci"] = dict(ci)  # never the caller's nested dict
                if _ci_conclusion(ci) == "failure":
                    # When the head turned red: the release of an item parked on it counts from here.
                    ci.setdefault("failed_at", now.isoformat())
                else:
                    # Green, or a re-run in progress: no longer red.
                    ci.pop("failed_at", None)
        elif event.kind == "head":
            if event.head_sha and event.head_sha != binding.get("head_sha"):
                binding["head_sha"] = event.head_sha
                binding["ci"] = {"head_sha": event.head_sha}
            if event.head_branch:
                binding["head_branch"] = event.head_branch
            if event.draft is not None:
                binding["draft"] = event.draft
        elif event.kind == "dequeued":
            done = _apply_dequeued(binding, event)
            if done is not None:
                outcomes.append(done)
        elif event.kind == "closed":
            if binding.get("state", "open") != "open":
                # Already closed: the delivery and the polling fallback may both
                # read the same closing, the second one changes nothing (#11045).
                continue
            newly_closed = True
            binding["state"] = "merged" if event.merged else "closed"
            binding["closed_at"] = now.isoformat()
            if event.merge_sha:
                binding["merge_sha"] = event.merge_sha
            if event.url and not binding.get("url"):
                binding["url"] = event.url
    if not touched or (event.kind == "closed" and not newly_closed):
        return out, []
    out["pull_requests"] = bindings
    if event.kind == "closed":
        all_merged = all(b.get("state") == "merged" for b in bindings)
        for number in event.pr_numbers:
            outcomes.append(
                PullRequestClosed(
                    repo=event.repo,
                    number=number,
                    merged=event.merged,
                    merge_sha=event.merge_sha,
                    url=event.url,
                    all_merged=all_merged,
                )
            )
    return out, outcomes


# -- Recording a binding -------------------------------------------------------------


async def record_pull_request(
    work_item_id: str,
    *,
    user_id: str | None,
    writer: Any,
    repo: str,
    number: int,
    head_branch: str | None = None,
    head_sha: str | None = None,
    url: str | None = None,
    draft: bool | None = None,
) -> dict | None:
    """Name the pull request on the work item (the item's own write rules apply).

    Returns the item, ``None`` when it is not found or not writable by *writer*.
    Raises :class:`ValueError` for a pull request that is not one.
    """
    from app.services.work_item import get_work_item, update_work_item

    item = await get_work_item(work_item_id, user_id=user_id)
    if item is None:
        return None
    payload = bind_pull_request(
        item.get("payload"),
        repo=repo,
        number=number,
        head_branch=head_branch,
        head_sha=head_sha,
        url=url,
        draft=draft,
    )
    updated = await update_work_item(work_item_id, user_id=user_id, payload=payload, writer=writer)
    if updated is not None:
        await carry_dependants_to_deployment(str(updated.get("id") or work_item_id), repo=repo, number=number)
    return updated


def waits_for_deployment(payload: object) -> bool:
    """Whether an item with *payload* asked its ``blocked_on`` items to release it at their build.

    ``blocked_on`` releases at the completion of the items it names; the
    deployment of their pull requests is waited for only when the waiter says
    ``blocked_on_release_on: deployed`` (#11919). Pure.
    """
    from app.services.work_item import RELEASE_ON_KEY

    return isinstance(payload, dict) and payload.get(RELEASE_ON_KEY) == "deployed"


async def carry_dependants_to_deployment(work_item_id: str, *, repo: str, number: int) -> int:
    """Make every item blocked on *work_item_id* that asked for it wait for the deployment of its pull request.

    Only an item whose payload says ``blocked_on_release_on: deployed`` is
    carried (:func:`waits_for_deployment`, #11919); any other is released at
    the completion of *work_item_id*, the contract of ``blocked_on``.

    A work item completes at its pull request's merge, before any build carries
    it, and the dependency sweep then released the items waiting for the fix too
    early (#9943). On a repository whose deployments the platform observes, each
    blocked item whose ``blocked_on`` names *work_item_id* gets a deployment
    reference on *repo*#*number* (``carried_by`` the item) and a dated note; its
    ``blocked_on`` is untouched, so the sweep drops the dependency at the
    completion and the first build carrying the merge releases it
    (:func:`~app.services.work_item_blocked_refs.on_release_deployed`). An item
    already waiting for that deployment, or at the reference cap, is left as it
    is. A system write, like the dependency sweep: best-effort, never raises.
    Returns how many items were carried.
    """
    from sqlalchemy.dialects.postgresql import JSONB

    from app.core.database import async_session
    from app.core.json_sql import when_json_parseable
    from app.services.work_item_blocked_refs import (
        MAX_REFS,
        BlockedRefError,
        carried_deployment_ref,
        carried_note,
        normalize_blocked_on_refs,
    )

    target = safe_uuid(str(work_item_id).strip())
    try:
        ref = carried_deployment_ref(repo, number, str(target)) if target is not None else None
    except BlockedRefError:
        return 0
    if ref is None:
        return 0
    now = _now()
    carried = 0
    try:
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(WorkItem)
                        .where(
                            WorkItem.status == "blocked",
                            WorkItem.blocked_on.is_not(None),
                            when_json_parseable(
                                WorkItem.blocked_on, cast(WorkItem.blocked_on, JSONB).contains([str(target)])
                            ),
                        )
                        .limit(_MAX_ITEMS_PER_EVENT)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for item in rows:
                if not waits_for_deployment(item.payload):
                    continue
                refs = [dict(r) for r in item.blocked_on_refs or [] if isinstance(r, dict)]
                if len(refs) >= MAX_REFS or any(
                    r.get("kind") == "deployment" and r.get("repo") == ref["repo"] and r.get("number") == ref["number"]
                    for r in refs
                ):
                    continue
                item.blocked_on_refs = normalize_blocked_on_refs([*refs, ref])
                text = carried_note(ref, now)
                item.claim_notes = [*(item.claim_notes or []), _note(now, text)]
                previous = item.description or ""
                item.description = f"{text}\n\n{previous}" if previous else text
                carried += 1
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Dependants of work item %s not carried to %s#%s: %s",
            work_item_id,
            repo,
            number,
            format_exception_message(exc),
        )
        return 0
    return carried


async def carry_to_blockers_pull_requests(work_item_id: str) -> int:
    """Make *work_item_id*, just blocked on items, wait for the deployment of their pull requests when it asked for it.

    Nothing is carried unless its payload says ``blocked_on_release_on:
    deployed`` (:func:`waits_for_deployment`, #11919).

    :func:`carry_dependants_to_deployment` runs when a pull request is named on
    the item it fixes; an item blocked on that item afterwards missed it and
    was released at the merge, before any build carried it (#11711). Each open
    or merged pull request its blockers name on an observed repository becomes
    a carried deployment reference here, with the same note. A pull request
    closed unmerged is skipped. Best-effort, never raises. Returns how many
    references were added.
    """
    from app.core.database import async_session
    from app.services.work_item_blocked_refs import (
        MAX_REFS,
        BlockedRefError,
        carried_deployment_ref,
        carried_note,
        normalize_blocked_on_refs,
    )

    target = safe_uuid(str(work_item_id).strip())
    if target is None:
        return 0
    now = _now()
    added = 0
    try:
        async with async_session() as db:
            item = (
                await db.execute(select(WorkItem).where(WorkItem.id == target).with_for_update())
            ).scalar_one_or_none()
            if (
                item is None
                or item.status != "blocked"
                or not item.blocked_on
                or not waits_for_deployment(item.payload)
            ):
                return 0
            ids = [u for u in (safe_uuid(str(b)) for b in item.blocked_on) if u is not None]
            blockers = (await db.execute(select(WorkItem.id, WorkItem.payload).where(WorkItem.id.in_(ids)))).all()
            refs = [dict(r) for r in item.blocked_on_refs or [] if isinstance(r, dict)]
            notes = list(item.claim_notes or [])
            texts: list[str] = []
            for blocker_id, payload in blockers:
                for binding in pull_request_bindings(payload):
                    if binding.get("state", "open") not in ("open", "merged"):
                        continue
                    try:
                        ref = carried_deployment_ref(binding["repo"], binding["number"], str(blocker_id))
                    except BlockedRefError:
                        ref = None
                    if ref is None or len(refs) >= MAX_REFS:
                        continue
                    if any(
                        r.get("kind") == "deployment"
                        and r.get("repo") == ref["repo"]
                        and r.get("number") == ref["number"]
                        for r in refs
                    ):
                        continue
                    refs.append(ref)
                    text = carried_note(ref, now)
                    notes.append(_note(now, text))
                    texts.append(text)
            if not texts:
                return 0
            item.blocked_on_refs = normalize_blocked_on_refs(refs)
            item.claim_notes = notes
            previous = item.description or ""
            joined = "\n".join(texts)
            item.description = f"{joined}\n\n{previous}" if previous else joined
            added = len(texts)
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Work item %s not carried to its blockers' pull requests: %s", work_item_id, format_exception_message(exc)
        )
        return 0
    from app.services.work_item_blocked_refs import schedule_deployment_check

    # A merge already live releases it now, as at any deployment reference write.
    schedule_deployment_check(str(target))
    return added


# -- Handling a delivery -----------------------------------------------------------


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _candidate_ids(db, event: PullRequestEvent) -> list[uuid.UUID]:
    """Unfinished items whose payload mentions the repository (narrowed in Python)."""
    rows = await db.execute(
        select(WorkItem.id, WorkItem.payload)
        .where(
            WorkItem.status.in_(_UNFINISHED),
            WorkItem.action_mode.is_(None),
            cast(WorkItem.payload, Text).ilike(f"%{_like_literal(event.repo)}%", escape="\\"),
        )
        .limit(_MAX_ITEMS_PER_EVENT * 4)
    )
    out: list[uuid.UUID] = []
    for item_id, payload in rows.all():
        if any(_matches(b, event) for b in pull_request_bindings(payload)):
            out.append(item_id)
    return out[:_MAX_ITEMS_PER_EVENT]


def _ci_note(done: CiCompleted, now: datetime, tail: str) -> dict:
    run = f", run {done.run_url}" if done.run_url else ""
    return _note(
        now,
        f"CI completed: {done.conclusion} on {done.repo}#{done.number}"
        f" at {(done.head_sha or '?')[:12]}{run} ({_stamp(now)}); {tail}",
    )


def released_on_closing(payload: dict | None, outcomes: list) -> bool:
    """Whether a closing in *outcomes* left no open pull request in *payload*. Pure.

    An item parked on its pull requests alone (:func:`parked_without_release`)
    waits for their CI or their end: once the last one is merged or closed,
    nothing else would ever hand it back (#10792).
    """
    if not any(isinstance(o, PullRequestClosed) for o in outcomes):
        return False
    return not any(b.get("state", "open") == "open" for b in pull_request_bindings(payload))


def closing_release_note(outcomes: list, now: datetime) -> str:
    """The note of an item released because the pull requests it was parked on closed."""
    named = ", ".join(
        f"{o.repo}#{o.number} {'merged' if o.merged else 'closed without merge'}"
        for o in outcomes
        if isinstance(o, PullRequestClosed)
    )
    return f"released: {named}, the pull request it was parked on is no longer open; back in the queue ({_stamp(now)})"


def _closed_note(closed: PullRequestClosed, now: datetime, completed: bool) -> dict:
    what = f"merged at {closed.merge_sha}" if closed.merged else "closed without merge"
    tail = "; item completed (complete_on_merge)" if completed else ""
    return _note(now, f"{closed.repo}#{closed.number} {what} ({_stamp(now)}){tail}")


async def _holder_alive(db, item: WorkItem) -> bool:
    from app.services.work_item import holding_runs

    run_id = getattr(item, "claimed_by_run_id", None)
    if run_id is None:
        # A claim made outside a run (a chat turn): its holder is working on it.
        return item.status in _HELD
    return holding_runs_alive(await holding_runs(db, [run_id]), run_id)


def holding_runs_alive(holders: dict, run_id: uuid.UUID) -> bool:
    holder = holders.get(run_id)
    return bool(holder is not None and holder.alive)


async def _owner_inbox(db, item: WorkItem):
    """The owner's first ``work_item`` assignment inbox that takes the item."""
    from app.services.work_item import accepted_by_inbox, load_assignment_inboxes

    if item.owner_agent_id is None:
        return None
    for inbox in await load_assignment_inboxes(db):
        if safe_uuid(inbox.owner_agent_id) != item.owner_agent_id:
            continue
        if item.id in await accepted_by_inbox(db, inbox, [item.id]):
            return inbox
    return None


async def _dispatch(item_id: uuid.UUID, done: CiCompleted, now: datetime) -> str:
    """Hand the item to its owner's inbox now; what happened, for the note."""
    from app.core.database import async_session
    from app.services.assignment_engine import has_active_run, trigger_assignment
    from app.services.shared_state_pull_requests import live_pull_request_lease
    from app.services.work_item import (
        bounded_trigger_item,
        clear_red_deferral,
        get_work_item,
        publish_transition,
        record_claim_run,
        release_work_item_claim,
        waits_on_items,
    )

    async with async_session() as db:
        item = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None or item.status not in ("open", "blocked"):
            return "not dispatched: the item is no longer waiting"
        if waits_on_items(item):
            return "not dispatched: it waits for other items"
        # The deferral of an unchanged red waited for this very CI conclusion.
        if clear_red_deferral(item):
            item.last_activity_at = now
            await db.commit()
        lease = await live_pull_request_lease(item.payload, user_id=getattr(item, "user_id", None))
        if lease is not None:
            # A live run holds a lease on its pull request: that run is pushing
            # the fix, so a run started now would only stand down. Left open and
            # unclaimed: the inbox pass takes it once the lease is released or
            # its holder ended (#10737, #11480).
            if item.status == "blocked":
                item.status = "open"
                item.last_activity_at = now
                await db.commit()
                await publish_transition(item_id, previous_status="blocked")
            return (
                f"not dispatched: run {lease['run_id']} holds {lease['key']} on "
                f"{lease['repo']}#{lease['number']}; the inbox takes it once released"
            )
        inbox = await _owner_inbox(db, item)
        if inbox is None:
            return "not dispatched: no work_item inbox of its owner takes it"
        assignment, agent = inbox.source
        carrier = await _live_red_carrier(db, agent.id, item.id, done)
        if carrier is not None:
            # A live run of the same agent already diagnoses this red head (a red
            # listener keyed on the head, or another item's dispatch): one
            # diagnosis per red (#11809). Left open and unclaimed, like a lease:
            # the inbox pass takes it once that run ended.
            if item.status == "blocked":
                item.status = "open"
                item.last_activity_at = now
                await db.commit()
                await publish_transition(item_id, previous_status="blocked")
            from app.services.ci_red_claims import standing_down_note

            return standing_down_note(carrier, done.repo, done.number, done.head_sha)
        if await has_active_run(db, assignment.id):
            # The inbox is serial: its next free tick takes the item.
            if item.status == "blocked":
                item.status = "open"
                item.last_activity_at = now
                await db.commit()
                await publish_transition(item_id, previous_status="blocked")
            return f"the inbox '{inbox.name}' is busy: picked up on its next free tick"
        previous_status = item.status
        item.status = "claimed"
        item.claimed_by_agent_id = agent.id
        item.claimed_by_run_id = None
        item.claimed_at = now
        item.last_activity_at = now
        await db.commit()
        user_id = inbox.user_id
        writer = inbox.writer
    await publish_transition(item_id, previous_status=previous_status, claimed_by_agent_id=agent.id)

    serialized = await get_work_item(str(item_id), user_id=user_id)
    try:
        async with async_session() as db:
            run_id = await trigger_assignment(
                assignment=assignment,
                agent=agent,
                payload={
                    "trigger_type": "work_item",
                    "work_item_id": str(item_id),
                    "work_item": bounded_trigger_item(serialized),
                },
                db=db,
                trigger_context={
                    "type": "work_item",
                    "work_item_id": str(item_id),
                    "correlation_key": (serialized or {}).get("correlation_key"),
                    "triggered_at": now.isoformat(),
                    "reason": CI_COMPLETED_REASON,
                    "conclusion": done.conclusion,
                    "run_url": done.run_url,
                    "pull_request": {
                        "repo": done.repo,
                        "number": done.number,
                        "head_sha": done.head_sha,
                    },
                },
                skip_if_active=True,
            )
    except Exception as exc:
        await release_work_item_claim(work_item_id=str(item_id), user_id=user_id, writer=writer)
        return f"not dispatched ({format_exception_message(exc)}): back in the queue"
    if run_id is None:
        await release_work_item_claim(work_item_id=str(item_id), user_id=user_id, writer=writer)
        return f"the inbox '{inbox.name}' is busy: picked up on its next free tick"
    await record_claim_run(str(item_id), agent_id=agent.id, run_id=run_id)
    return f"dispatched to '{inbox.name}': run {run_id}"


async def _live_red_carrier(db, agent_id, item_id: uuid.UUID, done: CiCompleted) -> uuid.UUID | None:
    """The live run of *agent_id* already diagnosing the red *done* reports, for a failure only (#11809)."""
    if done.conclusion != "failure" or not done.head_sha:
        return None
    from app.services.ci_red_claims import live_red_carrier

    return await live_red_carrier(db, agent_id, done.repo, done.head_sha, exclude_item_id=item_id)


def reconcile_failure(done: CiCompleted, state: dict[str, Any] | None) -> tuple[CiCompleted, str | None]:
    """A delivered *failure* checked against the live read *state* of its head (#11766). Pure.

    The delivery counts every check of the head; the live read decides on the
    checks the base branch requires (:func:`~app.integrations.providers.github.reference_state.head_ci_decision`).
    On the same head, a live ``success`` turns the dispatch green, and an
    undecided read (a required check still running, the red on a check the
    base does not require) dispatches nothing: the answer's second value says
    why. A live head the pull request moved to past the delivered one also
    dispatches nothing: that red is spent, the new head's CI decides (#2014).
    No read, or a live ``failure`` on the same head: the delivery stands.
    """
    if done.conclusion != "failure" or not isinstance(state, dict):
        return done, None
    label = f"{done.repo}#{done.number}"
    live_sha = state.get("head_sha")
    if isinstance(live_sha, str) and live_sha and done.head_sha and live_sha != done.head_sha:
        return done, (
            f"not dispatched: the red on {label} was on {done.head_sha[:12]}, the pull request moved to "
            f"{live_sha[:12]} (read live); the new head's CI completion wakes it"
        )
    if live_sha != done.head_sha:
        return done, None
    live = state.get("conclusion")
    if live == "failure":
        return done, None
    if live == "success":
        return (
            CiCompleted(
                repo=done.repo,
                number=done.number,
                head_sha=done.head_sha,
                conclusion="success",
                run_url=state.get("run_url") or done.run_url,
                run_name="checks of the head (read)",
                dispatch_key=done.dispatch_key,
            ),
            None,
        )
    if state.get("coverage_pending"):
        return done, (
            f"not dispatched: the red on {label} ({done.run_name or 'a check'}) is a coverage grade read before "
            "the head's other checks completed (read live); their completion decides it (#12050)"
        )
    return done, (
        f"not dispatched: the red on {label} ({done.run_name or 'a check'}) is not a check its base branch "
        "requires and the required ones have not all completed (read live); their completion wakes it"
    )


async def confirm_failure_on_required(done: CiCompleted, user_id: Any) -> tuple[CiCompleted, str | None, dict | None]:
    """*done* confirmed by one live read of its pull request when it is a failure (:func:`reconcile_failure`).

    The third value is the live read (``None`` when none was taken).
    """
    if done.conclusion != "failure" or user_id is None:
        return done, None, None
    reads = await read_live_ci([{"repo": done.repo, "number": done.number}], user_id)
    state = reads.get((done.repo, done.number))
    confirmed, skipped = reconcile_failure(done, state)
    return confirmed, skipped, state


def record_unconfirmed_failure(payload: object, done: CiCompleted, state: dict | None) -> tuple[dict, str | None]:
    """*payload* with a delivered failure the live read did not confirm on a required check recorded so. Pure.

    *done* is the delivered failure, *state* the live read of its head. A live
    ``success`` on the same head becomes the head's recorded CI; an undecided
    one (required checks still running, the red on a check the base does not
    require) marks the recorded failed runs ``not_required``. Either way the
    head no longer reads red (:func:`red_pull_requests`), so neither the red
    self-block release nor the inbox red guard acts on it (#11807). A live
    ``failure`` or no read: unchanged, and ``None``. A live head past the
    delivered one: the binding takes the live head and forgets the old CI, so
    the spent red neither releases nor parks the item (#2014).
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    if done.conclusion != "failure" or not isinstance(state, dict):
        return out, None
    live_sha = state.get("head_sha")
    if isinstance(live_sha, str) and live_sha and done.head_sha and live_sha != done.head_sha:
        bindings = pull_request_bindings(out)
        for binding in bindings:
            if (binding["repo"], binding["number"]) != (done.repo, done.number):
                continue
            recorded_ci: dict[str, Any] = binding["ci"] if isinstance(binding.get("ci"), dict) else {}
            if recorded_ci.get("head_sha") == live_sha:
                return out, None  # the new head's CI is recorded already
            binding["head_sha"] = live_sha
            binding["ci"] = {"head_sha": live_sha}
            out["pull_requests"] = bindings
            return out, f"{done.repo}#{done.number} moved from {done.head_sha[:12]} to {live_sha[:12]}"
        return out, None
    if state.get("head_sha") != done.head_sha:
        return out, None
    live = state.get("conclusion")
    if live == "failure":
        return out, None
    bindings = pull_request_bindings(out)
    label = f"{done.repo}#{done.number}"
    for binding in bindings:
        if (binding["repo"], binding["number"]) != (done.repo, done.number):
            continue
        ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
        if ci.get("head_sha") and ci.get("head_sha") != done.head_sha:
            return out, None
        if live != "success" and state.get("coverage_pending"):
            # A coverage grade read before its uploads (#12050): its red stays a candidate that
            # decides once every run of the head concluded, so its dispatch key is given back.
            runs = dict(ci.get("runs") or {}) if isinstance(ci.get("runs"), dict) else {}
            for key, run in list(runs.items()):
                if _concluded(run) and (run.get("conclusion") or "") not in _PASSING:
                    runs[key] = {**run, "provisional": True}
            ci["runs"] = runs
            ci["dispatched"] = [k for k in ci.get("dispatched") or [] if k != done.dispatch_key]
            ci.pop("failed_at", None)
            binding["ci"] = ci
            out["pull_requests"] = bindings
            return out, f"{label}: the red is a coverage grade read before its uploads; the head's completion decides"
        if live == "success":
            ci["runs"] = {
                POLLED_RUN_KEY: {
                    "run_id": None,
                    "name": "checks of the head (read)",
                    "status": "completed",
                    "conclusion": "success",
                    "url": state.get("run_url"),
                }
            }
            what = f"{label} is green on the checks its base branch requires"
        else:
            runs = dict(ci.get("runs") or {}) if isinstance(ci.get("runs"), dict) else {}
            for key, run in list(runs.items()):
                if _concluded(run) and (run.get("conclusion") or "") not in _PASSING:
                    runs[key] = {**run, "not_required": True}
            ci["runs"] = runs
            # Its dispatch key is given back: a later red of a required check on the same
            # head builds the same key, and would be dropped as a duplicate otherwise.
            ci["dispatched"] = [k for k in ci.get("dispatched") or [] if k != done.dispatch_key]
            what = f"{label}: the red is on a check its base branch does not require"
        ci.pop("failed_at", None)
        binding["ci"] = ci
        out["pull_requests"] = bindings
        return out, what
    return out, None


async def _apply_to_item(item_id: uuid.UUID, event: PullRequestEvent, now: datetime) -> list[str]:
    from app.core.database import async_session
    from app.services.work_item import publish_transition, waits_on_items

    to_dispatch: list[CiCompleted] = []
    actions: list[str] = []
    previous_status: str | None = None
    user_id: Any = None
    async with async_session() as db:
        # Row lock: the completions of a head's workflows arrive together, and a
        # lost update would forget a run.
        item = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None or item.status not in _UNFINISHED:
            return []
        previous_status = item.status
        user_id = getattr(item, "user_id", None)
        # Parked on its pull requests alone (a reason, no waker): read before the
        # event marks a binding closed, since that check counts the open ones.
        parked = parked_without_release(item)
        payload, outcomes = apply_pull_request_event(item.payload, event, now)
        item.payload = payload
        notes = list(item.claim_notes or [])
        for outcome in outcomes:
            if isinstance(outcome, CiCompleted):
                if item.status in _HELD and await _holder_alive(db, item):
                    notes.append(_ci_note(outcome, now, "the run holding the item is told"))
                    actions.append("noted")
                    # Told, not dispatched: the run's end must not park the item on it (#11730).
                    item.payload = _mark_told_holder(item.payload, outcome)
                elif waits_on_items(item):
                    notes.append(_ci_note(outcome, now, "it stays blocked until the items it waits for are done"))
                    actions.append("noted")
                else:
                    to_dispatch.append(outcome)
            else:
                complete = bool(outcome.merged and outcome.all_merged and payload.get("complete_on_merge") is True)
                notes.append(_closed_note(outcome, now, complete))
                if complete:
                    item.status = "done"
                    item.result = f"Completed on merge: {outcome.repo}#{outcome.number} merged at {outcome.merge_sha}"
                    actions.append("completed")
                else:
                    actions.append("noted")
        released_note: str | None = None
        if parked and item.status == "blocked" and released_on_closing(payload, outcomes):
            released_note = closing_release_note(outcomes, now)
            notes.append(_note(now, released_note))
            previous = item.description or ""
            item.description = f"{released_note}\n\n{previous}" if previous else released_note
            item.status = "open"
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            item.blocked_reason = None
            actions.append("released")
        item.claim_notes = notes
        if outcomes:
            item.last_activity_at = now
        completed = item.status == "done" and previous_status != "done"
        await db.commit()
    if completed:
        # Completed on merge: the item's status/completed events.
        await publish_transition(item_id, previous_status=previous_status)
    elif released_note is not None:
        # Back in the queue: the owner's inbox picks it up on this event.
        await publish_transition(item_id, previous_status="blocked", note=released_note)

    for done in to_dispatch:
        delivered = done
        done, skipped, state = await confirm_failure_on_required(done, user_id)
        if skipped is not None:
            actions.append("noted")
            what = skipped
        else:
            what = await _dispatch(item_id, done, now)
            actions.append("dispatched" if what.startswith("dispatched") else "queued")
        async with async_session() as db:
            item = (
                await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
            ).scalar_one_or_none()
            if item is not None:
                # The red the live read did not confirm is no red of the item any more (#11807).
                item.payload, _ = record_unconfirmed_failure(item.payload, delivered, state)
                item.claim_notes = [*(item.claim_notes or []), _ci_note(done, now, what)]
                await db.commit()
    return actions


# -- The CI polling fallback --------------------------------------------------------

#: An item parked on a pull request is read once quiet this long (its own run ended).
CI_POLL_MIN_AGE_SECONDS = 2 * 60
#: An item's pull requests are read again after this long, per process.
CI_POLL_QUIET_SECONDS = 3 * 60
#: Items one pass reads (two provider reads per pull request).
CI_POLL_MAX_ITEMS = 10
#: ``ci.runs`` key of a verdict the polling fallback read.
POLLED_RUN_KEY = "poll:checks"
#: Item id -> monotonic time of its last CI read, per process.
_ci_polled: dict[uuid.UUID, float] = {}


def apply_polled_ci(
    payload: dict | None, *, repo: str, number: int, state: dict, newer_than: datetime | None = None
) -> tuple[dict, CiCompleted | None]:
    """*payload* after a live read *state* of one pull request's head and CI, and the dispatch it owes. Pure.

    *state* is what a :data:`~app.integrations.reference_state.PullRequestCiReader`
    answers. A head the binding did not know (an update-branch merge, a push
    whose delivery never came) replaces the old one and forgets its CI, as a
    ``synchronize`` delivery would. A decided verdict is recorded as the head's
    CI and owes one dispatch, unless a delivery already dispatched the same head
    and conclusion. When *state* names the check run that decided
    (``check_id`` / ``completed_at``, :func:`~app.integrations.providers.github.reference_state.head_ci_decision`),
    a re-run that ends with a conclusion already dispatched on the same head is
    a new conclusion all the same: the binding remembers the last decider read
    (``ci.polled_decider``) and a different one owes a dispatch (#11185). So does
    a deciding check run completed after *newer_than* (the release a deferral
    on an unchanged red started at): a re-run that ended before the first read.
    A read that owes nothing leaves the recorded runs as they are, so the red
    signature an unchanged red is compared on does not move.
    """
    out = dict(payload or {})
    bindings = pull_request_bindings(out)
    wanted = (str(repo).strip().lower(), number)
    binding = next((b for b in bindings if (b["repo"], b["number"]) == wanted), None)
    sha = state.get("head_sha") if isinstance(state, dict) else None
    if binding is None or binding.get("state", "open") != "open" or not isinstance(sha, str) or not sha:
        return out, None
    ci = dict(binding.get("ci") or {}) if isinstance(binding.get("ci"), dict) else {}
    if binding.get("head_sha") != sha or ci.get("head_sha") not in (None, sha):
        binding["head_sha"] = sha
        ci = {"head_sha": sha}
    if isinstance(state.get("head_branch"), str) and state["head_branch"]:
        binding["head_branch"] = state["head_branch"]
    if isinstance(state.get("draft"), bool):
        binding["draft"] = state["draft"]
    conclusion = state.get("conclusion")
    done: CiCompleted | None = None
    conflict_key = f"{sha}:{CONFLICT_CONCLUSION}:"
    if state.get("conflict") is True and conflict_key not in (ci.get("dispatched") or []):
        # A conflict with the base sends no CI event: the read is its only wake, once per head.
        ci["head_sha"] = sha
        ci["dispatched"] = [*(ci.get("dispatched") or []), conflict_key][-_MAX_DISPATCH_KEYS:]
        binding["ci"] = ci
        out["pull_requests"] = bindings
        return out, CiCompleted(
            repo=binding["repo"],
            number=binding["number"],
            head_sha=sha,
            conclusion=CONFLICT_CONCLUSION,
            run_url=binding.get("url"),
            run_name="conflict with the base (read)",
            dispatch_key=conflict_key,
        )
    if conclusion in ("success", "failure"):
        ci["head_sha"] = sha
        decider = polled_decider(state)
        dispatched = list(ci.get("dispatched") or [])
        seen = any(str(k).startswith(f"{sha}:{conclusion}:") for k in dispatched)
        previous = ci.get("polled_decider")
        if decider is not None:
            ci["polled_decider"] = decider
        # A conclusion this head never dispatched, or the same one decided again by
        # a check run the previous read did not see, or one completed after the
        # deferral began (a re-run).
        completed = _parse_time(state.get("completed_at"))
        key = f"{sha}:{conclusion}:{POLLED_RUN_KEY}" + (f"#{decider}" if decider is not None else "")
        rerun = (
            decider is not None
            and key not in dispatched
            and (
                (previous is not None and previous != decider)
                or (newer_than is not None and completed is not None and completed > newer_than)
            )
        )
        if not seen or rerun:
            # The live verdict of every check of the head: it decides alone. The
            # deciding check run stands for the run, so a re-run changes the red
            # signature an inbox release compares (work_item._red_signature).
            ci["runs"] = {
                POLLED_RUN_KEY: {
                    "run_id": decider,
                    "name": "checks of the head (read)",
                    "status": "completed",
                    "conclusion": conclusion,
                    "url": state.get("run_url"),
                }
            }
            ci["dispatched"] = [*dispatched, key][-_MAX_DISPATCH_KEYS:]
            done = CiCompleted(
                repo=binding["repo"],
                number=binding["number"],
                head_sha=sha,
                conclusion=conclusion,
                run_url=state.get("run_url"),
                run_name="checks of the head (read)",
                dispatch_key=key,
            )
    binding["ci"] = ci
    out["pull_requests"] = bindings
    return out, done


def _parse_time(value: Any) -> datetime | None:
    """An aware datetime from an ISO-8601 text (``Z`` accepted), ``None`` otherwise. Pure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def red_deferral_start(item: Any) -> datetime | None:
    """When the deferral of *item* on an unchanged red began (its release), ``None`` when it is not deferred. Pure."""
    from app.services.work_item import RED_DEFERRED_UNTIL_KEY, RED_UNCHANGED_DEFER

    payload = getattr(item, "payload", None)
    until = _parse_time(payload.get(RED_DEFERRED_UNTIL_KEY)) if isinstance(payload, dict) else None
    return until - RED_UNCHANGED_DEFER if until is not None else None


def polled_decider(state: dict) -> str | None:
    """What identifies the check run that decided a live read *state*, ``None`` when it names none. Pure."""
    check_id = state.get("check_id") if isinstance(state, dict) else None
    completed_at = state.get("completed_at") if isinstance(state, dict) else None
    if not isinstance(check_id, int) and not (isinstance(completed_at, str) and completed_at):
        return None
    return f"{check_id if isinstance(check_id, int) else ''}@{completed_at or ''}"


def red_deferred(item: Any) -> bool:
    """Whether *item* is back in the queue but deferred on an unchanged red, on an open pull request it names.

    :func:`~app.services.work_item.settle_inbox_work_item` defers an item
    released twice on the same red (#11163) until the next CI conclusion of its
    pull requests: the poll reads such an item too, so a re-run or a fix wakes
    it within a pass rather than an hour later (#11185). Pure.
    """
    from app.services.work_item import RED_DEFERRED_UNTIL_KEY

    if getattr(item, "status", None) != "open" or getattr(item, "blocked_on", None):
        return False
    payload = getattr(item, "payload", None)
    if not isinstance(payload, dict) or not payload.get(RED_DEFERRED_UNTIL_KEY):
        return False
    return any(b.get("state", "open") == "open" for b in pull_request_bindings(payload))


def ci_poll_candidate(item: Any) -> bool:
    """Whether the CI poll reads *item*: parked on its pull requests, or deferred on an unchanged red. Pure."""
    return parked_without_release(item) or red_deferred(item)


def parked_without_release(item: Any) -> bool:
    """Whether *item* is blocked with nothing a platform event releases, on an open pull request it names.

    No work item it waits for, no open reference, no person to answer: only
    its pull requests' CI completion can hand it back (a draft between two
    slices, #10692). Pure.
    """
    from app.services.work_item_blocked_refs import open_refs

    if getattr(item, "status", None) != "blocked" or getattr(item, "blocked_on", None):
        return False
    if getattr(item, "needs_human", None) or open_refs(getattr(item, "blocked_on_refs", None)):
        return False
    return any(b.get("state", "open") == "open" for b in pull_request_bindings(getattr(item, "payload", None)))


async def _apply_polled(item_id: uuid.UUID, reads: dict[tuple[str, int], dict], now: datetime) -> list[str]:
    """Record *reads* on one item under its row lock, then dispatch what they decided."""
    from app.core.database import async_session

    to_dispatch: list[CiCompleted] = []
    async with async_session() as db:
        item = (await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())).scalar_one_or_none()
        if item is None or not ci_poll_candidate(item):
            return []
        payload = item.payload
        since = red_deferral_start(item) if red_deferred(item) else None
        for (repo, number), state in reads.items():
            payload, done = apply_polled_ci(payload, repo=repo, number=number, state=state, newer_than=since)
            if done is not None:
                to_dispatch.append(done)
        item.payload = payload
        await db.commit()
    actions: list[str] = []
    for done in to_dispatch[:1]:
        # One dispatch per item: its run reads every pull request it names.
        what = await _dispatch(item_id, done, now)
        actions.append("dispatched" if what.startswith("dispatched") else "queued")
        async with async_session() as db:
            item = (
                await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
            ).scalar_one_or_none()
            if item is not None:
                note = _ci_note(done, now, f"read live, no CI delivery had come; {what}")
                item.claim_notes = [*(item.claim_notes or []), note]
                await db.commit()
    return actions


async def _apply_polled_closings(item_id: uuid.UUID, bindings: list[dict], user_id: Any, counts: dict) -> None:
    """Read the closings of *bindings* and apply each through the delivery's path. Never raises.

    A webhook delivery that never came, or came late, leaves an item parked on
    a merged pull request blocked: the closing read live goes through
    :func:`_apply_to_item`, the same path as the delivery, so the item is
    released within one pass. Idempotent with the delivery: a closing already
    recorded on the binding changes nothing (:func:`apply_pull_request_event`).
    """
    try:
        closings = await read_pull_request_closings(bindings, user_id)
        for event in closings:
            for action in await _apply_to_item(item_id, event, _now()):
                if action in ("released", "completed"):
                    counts[action] = counts.get(action, 0) + 1
    except Exception as exc:
        logger.warning("Polled closing could not move work item %s: %s", item_id, format_exception_message(exc))


async def poll_pull_request_ci(now: datetime | None = None) -> dict:
    """Hand back the items parked on a pull request whose CI completed with no delivery.

    An item blocked with only a reason (a draft between two slices) waits for
    its pull requests' CI completion, which reaches it only through a signed
    CI delivery for the head its binding knows. A missing delivery, an unsigned
    webhook or a head made by an update-branch merge left it blocked for good
    (#10692). Each pass reads the open pull requests of such items quiet for
    :data:`CI_POLL_MIN_AGE_SECONDS` through the provider's
    :data:`~app.integrations.reference_state.PullRequestCiReader`, with the
    credential the owner reads that provider with, and dispatches a decided
    verdict exactly as the delivery would (``pr_ci_completed``). A pull request
    the CI reader answers nothing for (closed, or unreadable) has its closing
    read with the :data:`~app.integrations.reference_state.ReferenceStateReader`
    and applied through the delivery's path, so a merge whose delivery never
    came still releases the item (#11045). An item back in the queue but
    deferred on an unchanged red (:func:`red_deferred`) is read the same way:
    a new conclusion clears the deferral and dispatches it (#11185).

    ``{"read": r, "dispatched": n, "queued": m}``, plus ``released`` and
    ``completed`` when a polled closing did either. Never raises.
    """
    import time
    from datetime import timedelta

    from app.core.database import async_session
    from app.integrations.reference_state import pull_request_ci_readers
    from app.services.work_item_blocked_refs import reference_token

    counts = {"read": 0, "dispatched": 0, "queued": 0}
    readers = pull_request_ci_readers()
    if not readers:
        return counts
    now = now or _now()
    now_mono = time.monotonic()
    try:
        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(WorkItem)
                        .where(
                            or_(
                                WorkItem.status == "blocked",
                                and_(
                                    WorkItem.status == "open",
                                    cast(WorkItem.payload, Text).like('%"_red_deferred_until"%'),
                                ),
                            ),
                            WorkItem.action_mode.is_(None),
                            WorkItem.user_id.is_not(None),
                            WorkItem.last_activity_at <= now - timedelta(seconds=CI_POLL_MIN_AGE_SECONDS),
                            cast(WorkItem.payload, Text).ilike('%"pull_requests"%'),
                        )
                        .order_by(WorkItem.last_activity_at.asc())
                        .limit(CI_POLL_MAX_ITEMS * 4)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [
                (
                    item.id,
                    item.user_id,
                    [b for b in pull_request_bindings(item.payload) if b.get("state", "open") == "open"],
                )
                for item in rows
                if ci_poll_candidate(item)
                and now_mono - _ci_polled.get(item.id, -CI_POLL_QUIET_SECONDS) >= CI_POLL_QUIET_SECONDS
            ][:CI_POLL_MAX_ITEMS]
            tokens: dict[tuple[str, str], str | None] = {}
            for _, user_id, _ in candidates:
                for provider in readers:
                    if (provider, str(user_id)) not in tokens:
                        tokens[(provider, str(user_id))] = await reference_token(db, provider, user_id)
    except Exception as exc:
        logger.warning("Pull request CI not polled: %s", format_exception_message(exc))
        return counts
    for item_id, user_id, bindings in candidates:
        _ci_polled[item_id] = now_mono
        reads: dict[tuple[str, int], dict] = {}
        unread: list[dict] = []
        for binding in bindings[:MAX_PULL_REQUESTS]:
            for provider, reader in readers.items():
                token = tokens.get((provider, str(user_id)))
                if not token:
                    continue
                counts["read"] += 1
                try:
                    state = await reader(binding["repo"], binding["number"], token)
                except Exception as exc:
                    logger.info(
                        "Pull request %s#%s CI not read: %s",
                        binding["repo"],
                        binding["number"],
                        format_exception_message(exc),
                    )
                    state = None
                if state is not None:
                    reads[(binding["repo"], binding["number"])] = state
                    break
            if (binding["repo"], binding["number"]) not in reads:
                unread.append(binding)
        if unread:
            # The CI reader answers nothing for a closed pull request: read its
            # closing and apply it as the delivery would, which releases an item
            # whose last open pull request it was (#11045).
            await _apply_polled_closings(item_id, unread, user_id, counts)
        if not reads:
            continue
        try:
            for action in await _apply_polled(item_id, reads, _now()):
                counts[action] = counts.get(action, 0) + 1
        except Exception as exc:
            logger.warning("Polled CI could not move work item %s: %s", item_id, format_exception_message(exc))
    if len(_ci_polled) > CI_POLL_MAX_ITEMS * 40:
        for stale in [k for k, t in _ci_polled.items() if now_mono - t >= CI_POLL_QUIET_SECONDS]:
            _ci_polled.pop(stale, None)
    if counts["dispatched"] or counts["queued"] or counts.get("released") or counts.get("completed"):
        logger.info("Pull request CI polled: %s", counts)
    return counts


async def handle_pull_request_event(event: PullRequestEvent) -> dict:
    """Apply *event* to every unfinished work item naming its pull request.

    Returns ``{"items": n, "actions": [...]}``. Never raises.
    """
    from app.core.database import async_session
    from app.services.work_item_blocked_refs import release_blocked_on_refs, surface_on_dequeue

    released: dict = {}
    if event.kind in ("closed", "issue_closed"):
        # The items blocked on this pull request or issue go back to the queue.
        released = await release_blocked_on_refs(event)
    elif event.kind == "dequeued":
        # The items blocked on its merge by reference alone: no closing will come (#397).
        released = await surface_on_dequeue(event)
    if event.kind == "issue_closed":
        return {"items": 0, "actions": [], "released": released}
    now = _now()
    try:
        async with async_session() as db:
            ids = await _candidate_ids(db, event)
    except Exception as exc:
        logger.warning("Pull request event on %s: candidates unreadable: %s", event.repo, format_exception_message(exc))
        return {"items": 0, "actions": [], "released": released}
    actions: list[str] = []
    for item_id in ids:
        try:
            actions += await _apply_to_item(item_id, event, now)
        except Exception as exc:
            logger.warning(
                "Pull request event on %s could not move work item %s: %s",
                event.repo,
                item_id,
                format_exception_message(exc),
            )
    if actions:
        logger.info("Pull request event %s on %s moved %d work item(s): %s", event.kind, event.repo, len(ids), actions)
    return {"items": len(ids), "actions": actions, "released": released}
