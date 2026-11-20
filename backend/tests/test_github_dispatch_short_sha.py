"""dispatch_github_workflow sends a full SHA for an abbreviated one in a ref or sha input (#642).

A workflow that hands such an input to actions/checkout fetches it as a branch
name: ``ref: cd3a70e2`` failed the fetch and read as a red run on main.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
_FULL = "cd3a70e2" + "0" * 32


def _dispatch_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["dispatch_github_workflow"]


class _GitHub:
    def __init__(self, commit_status: int = 200):
        self.payloads: list[dict] = []
        self.commit_reads: list[str] = []
        self.commit_status = commit_status

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method == "post":
            self.payloads.append(kwargs.get("json"))
            return httpx.Response(204), None
        if "/commits/" in url:
            self.commit_reads.append(url.rsplit("/", 1)[-1])
            if self.commit_status != 200:
                return None, "Error: GitHub resource not found while trying to resolve commit"
            return httpx.Response(200, json={"sha": _FULL}), None
        return httpx.Response(200, json={"workflow_runs": []}), None


@pytest.mark.asyncio
async def test_short_sha_ref_input_is_sent_as_the_full_sha():
    github = _GitHub()
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="e2e.yml", ref="main", inputs={"ref": "cd3a70e2"})
    assert github.payloads == [{"ref": "main", "inputs": {"ref": _FULL}}]
    assert "expanded from cd3a70e2" in answer


@pytest.mark.asyncio
async def test_unresolvable_short_sha_refuses_the_dispatch():
    github = _GitHub(commit_status=404)
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(repo="o/r", workflow_id="e2e.yml", ref="main", inputs={"head_sha": "abc1234"})
    assert answer.startswith("Not dispatched")
    assert github.payloads == []


@pytest.mark.asyncio
async def test_other_inputs_and_full_shas_are_left_alone():
    github = _GitHub()
    inputs = {"ref": _FULL, "build": "deadbeef", "branch_ref": "main"}
    with patch(_SAFE_REQUEST, new=github):
        await _dispatch_tool()(repo="o/r", workflow_id="e2e.yml", ref="main", inputs=inputs)
    assert github.commit_reads == []
    assert github.payloads[0]["inputs"] == inputs
