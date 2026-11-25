"""A CI completion held back is not dispatched once its pull request's head moved (#2243).

A completion deferred by a live lease or a busy inbox reaches ``_dispatch``
later; when the item's binding records a newer head by then, the completion is
stale: the new head's own CI completion hands the item back.
"""

from __future__ import annotations

from typing import Any

from app.services import work_item_pull_requests as prs

REPO = "xtm-one-platform/xtm-one"
OLD = "2e2d3099" + "0" * 32
NEW = "5a525d46" + "0" * 32


def _done(head_sha: str | None = OLD, number: int = 12) -> prs.CiCompleted:
    return prs.CiCompleted(
        repo=REPO,
        number=number,
        head_sha=head_sha,
        conclusion="failure",
        run_url=None,
        run_name=None,
        dispatch_key=f"{head_sha}:failure:x",
    )


def _payload(head_sha: str | None) -> dict[str, Any]:
    binding: dict[str, Any] = {"repo": REPO, "number": 12, "state": "open"}
    if head_sha:
        binding["head_sha"] = head_sha
    return {"pull_requests": [binding]}


def test_a_completion_of_an_older_head_names_the_head_that_replaced_it() -> None:
    assert prs.moved_head(_payload(NEW), _done()) == NEW


def test_a_completion_of_the_current_head_is_current() -> None:
    assert prs.moved_head(_payload(OLD), _done()) is None


def test_an_unknown_head_on_either_side_is_never_called_moved() -> None:
    assert prs.moved_head(_payload(None), _done()) is None
    assert prs.moved_head(_payload(NEW), _done(head_sha=None)) is None


def test_another_pull_request_of_the_item_is_not_read() -> None:
    assert prs.moved_head(_payload(NEW), _done(number=13)) is None
