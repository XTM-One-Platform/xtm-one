# SPDX-License-Identifier: Apache-2.0

"""Pull requests whose full CI run is red on the base branch's own failure.

A repository may require a full CI run green on a pull request's head before
a merge (GitHub: ``[merge] full_ci_workflows``). When that run fails on the
same jobs the base branch's latest run fails on, the red is not the change's:
the merge gate parks the pull request (``services/full_ci_parking.py``), and
the base's next green run of that workflow runs it again on the head.

How a provider runs a workflow again on a pull request is its own API, so each
provider registers a redispatcher here and the generic layer never reads a
vendor API.

Light on purpose (no model, no service import), like ``reference_state``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: ``(repo, number, head_sha, workflow, token)`` -> one line saying what was done.
#: Brings the head up to date with its base when it can, then runs *workflow* on
#: it; leaves a closed pull request, or one whose head moved from *head_sha*, alone.
FullCiRedispatcher = Callable[[str, int, str, str, str], Awaitable[str]]

_REDISPATCHERS: dict[str, FullCiRedispatcher] = {}


def register_full_ci_redispatcher(provider: str, redispatcher: FullCiRedispatcher) -> None:
    """Register *provider*'s redispatcher. Idempotent (last wins)."""
    _REDISPATCHERS[provider] = redispatcher


def full_ci_redispatcher(provider: str) -> FullCiRedispatcher | None:
    """The redispatcher of *provider*, ``None`` when it registered none."""
    return _REDISPATCHERS.get(provider)
