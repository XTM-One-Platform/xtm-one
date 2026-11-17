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
  ready_for_review and converted_to_draft (the draft flag, on a ``head`` event);
* ``issues`` closed: an ``issue_closed`` event, which releases the
  work items blocked on the issue.

Every other delivery answers ``None``.
"""

from __future__ import annotations

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
