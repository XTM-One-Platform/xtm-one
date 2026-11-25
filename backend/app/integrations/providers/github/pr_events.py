"""GitHub deliveries as pull request events.

Turns the webhook events that move a pull request's CI or state into the
generic :class:`~app.integrations.pull_request_events.PullRequestEvent` the
work-item service reads:

* ``workflow_run`` (requested, in_progress, completed): one CI source per
  workflow of the head commit;
* ``check_suite`` (requested, rerequested, completed) of an app other than
  GitHub Actions, whose suites ``workflow_run`` already reports; advisory, as
  an app can complete green before the head's workflows are requested;
* ``pull_request`` closed (merged or not), synchronize (new head commit),
  ready_for_review and converted_to_draft (the draft flag, on a ``head`` event),
  dequeued without a merge (a ``dequeued`` event, the removal reason as its
  conclusion);
* ``issues`` closed: an ``issue_closed`` event, which releases the
  work items blocked on the issue;
* ``create`` of a tag: a ``tag_created`` event naming the tag, which hints the
  work items blocked on a ``tag`` reference (#2209).

Every other delivery answers ``None``.
"""

from __future__ import annotations

from typing import Any

from app.integrations.pull_request_events import PullRequestEvent

_ACTIONS_APP_SLUG = "github-actions"


def _repo(payload: dict) -> str | None:
    repo = payload.get("repository")
    name = repo.get("full_name") if isinstance(repo, dict) else None
    return name.strip().lower() if isinstance(name, str) and name.count("/") == 1 else None


def _numbers(pulls: object) -> tuple[int, ...]:
    if not isinstance(pulls, list):
        return ()
    out: list[int] = []
    for pull in pulls:
        number = pull.get("number") if isinstance(pull, dict) else None
        if isinstance(number, int) and number > 0:
            out.append(number)
    return tuple(out)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


#: Removal reasons of a merge queue entry that mean it merged: no wake owed.
_MERGED_REASONS = frozenset({"MERGE", "MERGED", "ALREADY_MERGED"})
#: Removal reasons that mean the queue's own CI run failed or timed out.
_CHECK_REASONS = frozenset({"FAILED_CHECKS", "CI_FAILURE", "CI_TIMEOUT"})


def queue_run_hint(repo: str, reason: str | None, removed_at: str | None) -> str | None:
    """Where the failing merge queue run of a removal is read, ``None`` when the reason is no CI failure. Pure.

    GitHub's ``dequeued`` delivery and the timeline removal name neither the
    merge group nor its run: the owner listed runs by hand to find it (gaps
    queue-eject-notice-no-run-id, dequeue-note-without-failing-run). The hint is
    the exact read that lists it, from the removal's day.
    """
    if (reason or "").upper() not in _CHECK_REASONS:
        return None
    day = (removed_at or "")[:10]
    created = f', created=">={day}"' if len(day) == 10 else ""
    return (
        f'the failing queue run: list_github_workflow_runs(repo="{repo}", event="merge_group", '
        f'status="failure"{created}), then list_github_workflow_jobs on its run_id'
    )


def _dequeued(repo: str, payload: dict[str, Any]) -> PullRequestEvent | None:
    """A ``pull_request`` ``dequeued`` delivery that did not merge, as a ``dequeued`` event."""
    raw = payload.get("pull_request")
    if not isinstance(raw, dict) or raw.get("merged"):
        return None
    pull: dict[str, Any] = raw
    number = _int(pull.get("number")) or _int(payload.get("number"))
    reason = (_str(payload.get("reason")) or "unknown").upper()
    if not number or reason in _MERGED_REASONS:
        return None
    raw_head = pull.get("head")
    head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
    removed_at = _str(pull.get("updated_at"))
    hint = queue_run_hint(repo, reason, removed_at)
    return PullRequestEvent(
        kind="dequeued",
        repo=repo,
        pr_numbers=(number,),
        head_branch=_str(head.get("ref")),
        head_sha=_str(head.get("sha")),
        conclusion=reason.lower(),
        url=_str(pull.get("html_url")),
        # When GitHub removed it: one wake per removal, a second removal of the same head is another.
        closed_at=removed_at,
        extra={"queue_run_hint": hint} if hint else {},
    )


def parse_github_pull_request_event(event_key: str, payload: dict) -> PullRequestEvent | None:
    event, _, action = event_key.partition(".")
    repo = _repo(payload)
    if repo is None:
        return None
    if event == "workflow_run" and action in ("requested", "in_progress", "completed"):
        run = payload.get("workflow_run")
        if not isinstance(run, dict):
            return None
        path = _str(run.get("path")) or ""
        return PullRequestEvent(
            kind="ci",
            repo=repo,
            pr_numbers=_numbers(run.get("pull_requests")),
            head_branch=_str(run.get("head_branch")),
            head_sha=_str(run.get("head_sha")),
            run_key=f"workflow:{run.get('workflow_id') or run.get('name')}",
            run_id=_int(run.get("id")),
            run_attempt=_int(run.get("run_attempt")),
            run_name=_str(run.get("name")),
            status=_str(run.get("status")),
            conclusion=_str(run.get("conclusion")),
            run_url=_str(run.get("html_url")),
            advisory=run.get("event") == "pull_request_target",
            # A run testing a branch itself (a push, a manual run), the base green
            # that runs the parked full CI again (services/full_ci_parking.py).
            extra={
                "base_run": run.get("event") in ("push", "workflow_dispatch", "schedule"),
                "workflow_file": path.rsplit("/", 1)[-1].split("@", 1)[0] if path else None,
            },
        )
    if event == "check_suite" and action in ("requested", "rerequested", "completed"):
        suite = payload.get("check_suite")
        if not isinstance(suite, dict):
            return None
        app = suite.get("app") if isinstance(suite.get("app"), dict) else {}
        if app.get("slug") == _ACTIONS_APP_SLUG:
            return None
        sha = _str(suite.get("head_sha"))
        html = (payload.get("repository") or {}).get("html_url")
        return PullRequestEvent(
            kind="ci",
            repo=repo,
            pr_numbers=_numbers(suite.get("pull_requests")),
            head_branch=_str(suite.get("head_branch")),
            head_sha=sha,
            run_key=f"check_suite:{app.get('id') or app.get('slug') or suite.get('id')}",
            run_id=_int(suite.get("id")),
            run_name=_str(app.get("name")),
            status=_str(suite.get("status")),
            conclusion=_str(suite.get("conclusion")),
            run_url=f"{html}/commit/{sha}/checks" if isinstance(html, str) and sha else None,
            advisory=True,
        )
    if event == "pull_request" and action in ("closed", "synchronize", "ready_for_review", "converted_to_draft"):
        pull = payload.get("pull_request")
        if not isinstance(pull, dict):
            return None
        number = _int(pull.get("number")) or _int(payload.get("number"))
        if not number:
            return None
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        # When it merged (or closed), as GitHub states it; a push has none.
        closed_at = None
        if action == "closed":
            closed_at = (_str(pull.get("merged_at")) if pull.get("merged") else None) or _str(pull.get("closed_at"))
        draft = pull.get("draft")
        return PullRequestEvent(
            kind="closed" if action == "closed" else "head",
            repo=repo,
            pr_numbers=(number,),
            head_branch=_str(head.get("ref")),
            head_sha=_str(head.get("sha")),
            merged=bool(pull.get("merged")),
            merge_sha=_str(pull.get("merge_commit_sha")) if pull.get("merged") else None,
            url=_str(pull.get("html_url")),
            closed_at=closed_at,
            draft=draft if isinstance(draft, bool) and action != "closed" else None,
        )
    if event == "pull_request" and action == "dequeued":
        return _dequeued(repo, payload)
    if event == "create" and not action and payload.get("ref_type") == "tag":
        tag = _str(payload.get("ref"))
        if not tag:
            return None
        return PullRequestEvent(kind="tag_created", repo=repo, extra={"tag": tag})
    if event == "issues" and action == "closed":
        issue = payload.get("issue")
        if not isinstance(issue, dict) or issue.get("pull_request"):
            return None
        number = _int(issue.get("number"))
        if not number:
            return None
        return PullRequestEvent(
            kind="issue_closed",
            repo=repo,
            pr_numbers=(number,),
            url=_str(issue.get("html_url")),
            closed_at=_str(issue.get("closed_at")),
            extra={"state_reason": _str(issue.get("state_reason"))},
        )
    return None
