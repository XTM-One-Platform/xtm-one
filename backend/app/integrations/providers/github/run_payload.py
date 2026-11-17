"""The GitHub ``push`` delivery as the run is shown it.

A ``push`` names each commit with its whole message (a squash merge carries the
pull request's body), repeats the last commit as ``head_commit`` and lists the
``*_url`` API templates of ``repository``, ``sender``, ``pusher`` and
``organization``: a squash of a long pull request came to 174,383 characters,
cut at the serialiser's cap. The run acts on ``repository.full_name``, ``ref``,
``before``, ``after`` and, per commit, its ``id``, the first line of its message
and its ``added`` / ``removed`` / ``modified`` paths.

The shaper returns a copy where:

- ``head_commit`` is dropped when it is the last commit of ``commits`` (its id
  stays in ``after``); a ``head_commit`` alone (a tag push, a ``commits`` cut
  by GitHub) is kept, shaped like a commit;
- each commit message is cut to its first line, at most
  :data:`MESSAGE_FIRST_LINE_MAX_CHARS` characters, with ``message_truncated``
  set when something was cut;
- every ``*_url`` key is dropped at any depth, except ``html_url`` (the link a
  run cites); ``compare`` is kept.

Paths are never cut. Every other event is returned unchanged.
"""

from __future__ import annotations

from typing import Any

#: The first line of a commit message kept for the run.
MESSAGE_FIRST_LINE_MAX_CHARS = 200
#: The URL key a run cites; every other ``*_url`` is an API template.
_KEPT_URL_KEYS = frozenset({"html_url"})


def _without_url_templates(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _without_url_templates(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.endswith("_url") and k not in _KEPT_URL_KEYS)
        }
    if isinstance(value, list):
        return [_without_url_templates(v) for v in value]
    return value


def _shape_commit(commit: Any) -> Any:
    if not isinstance(commit, dict):
        return commit
    shaped = dict(commit)
    message = shaped.get("message")
    if isinstance(message, str):
        first = message.split("\n", 1)[0].rstrip("\r")
        cut = first[:MESSAGE_FIRST_LINE_MAX_CHARS]
        shaped["message"] = cut
        if cut != message:
            shaped["message_truncated"] = True
    return shaped


def shape_github_run_payload(event_key: str, payload: dict) -> dict:
    """*payload* trimmed for the run when it is a ``push``, else unchanged."""
    if event_key.split(".", 1)[0] != "push" or not isinstance(payload, dict):
        return payload
    shaped = dict(payload)
    commits = shaped.get("commits")
    head = shaped.get("head_commit")
    if isinstance(commits, list):
        shaped["commits"] = [_shape_commit(c) for c in commits]
        last = commits[-1] if commits else None
        if (
            isinstance(head, dict)
            and isinstance(last, dict)
            and head.get("id")
            and head.get("id") == last.get("id")
        ):
            shaped.pop("head_commit", None)
        elif head is not None:
            shaped["head_commit"] = _shape_commit(head)
    elif head is not None:
        shaped["head_commit"] = _shape_commit(head)
    return _without_url_templates(shaped)
