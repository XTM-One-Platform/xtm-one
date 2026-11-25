"""create_github_commit(from_checkout) when the checkout record names its commit by a short id.

materialize_repository records the commit the archive's top directory names
(``<owner>-<repo>-<sha7>``): seven characters. The drift reconcile accepted a
full 40-character id only, so with a short one it returned the change set as
it was and the workspace files were written whole over a branch that had moved
since the checkout, silently reverting its commits (run cb803120: checkout of
main at 49f3bd3, branch cut from a newer main, #1669 lines dropped).
"""

import dataclasses
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.github import contents
from tests.test_github_commit import _SAFE_REQUEST, API, _commit_tool
from tests.test_github_commit_checkout_drift import (
    BASE_TEXT,
    BRANCH_TEXT,
    RECORDED,
    _committed,
    _DriftSink,
    _MovedBranch,
)

SHORT = RECORDED[:7]


class _ShortAware(_MovedBranch):
    """GitHub resolving the short id of RECORDED to its full id, and nothing else."""

    def __init__(self, *args, known: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.known = known

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method == "get" and url == f"{API}/commits/{SHORT}":
            self.calls.append((method, url))
            if not self.known:
                return httpx.Response(404, json={"message": "No commit found"}), "Error: GitHub returned HTTP 404"
            return httpx.Response(200, json={"sha": RECORDED, "commit": {"tree": {"sha": "8" * 40}}}), None
        return await super().__call__(client, method, url, provider, action, **kwargs)


def _short_sink(workspace: dict[str, bytes]) -> _DriftSink:
    sink = _DriftSink(workspace)
    sink.record = dataclasses.replace(sink.record, commit=SHORT)
    return sink


@pytest.mark.asyncio
async def test_a_short_record_commit_still_merges_with_a_moved_branch():
    commit, _ = _commit_tool()
    sink = _short_sink({"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nfive\n"})
    fake = _ShortAware({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT})
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    both = b"ONE (workspace)\ntwo\nthree\nfour\nFIVE (branch)\n"
    assert result.startswith("Committed 1 file change(s)"), result
    # On main the workspace text was written whole: the branch's FIVE line was reverted.
    assert _committed(fake) == {"src/app.py": both}
    assert "Merged with the branch" in result
    assert ("get", f"{API}/commits/{SHORT}") in fake.calls


@pytest.mark.asyncio
async def test_a_short_record_commit_github_cannot_read_refuses_instead_of_writing_whole():
    commit, _ = _commit_tool()
    sink = _short_sink({"src/app.py": b"ONE (workspace)\ntwo\nthree\nfour\nfive\n"})
    fake = _ShortAware({"src/app.py": BASE_TEXT}, {"src/app.py": BRANCH_TEXT}, known=False)
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "current_workspace", return_value=sink):
        result = await commit(repo="acme/app", message="feat(app): x", from_checkout="app", branch="feat/x")

    assert result.startswith(f"Error: the checkout record of app names commit {SHORT}"), result
    assert "nothing was committed" in result
    assert 'sandbox_checkout_rebase(checkout="app", ref="feat/x")' in result
    assert fake.commits == []
