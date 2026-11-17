"""dispatch_github_workflow sends inputs as GitHub's dispatch API expects them (#10225).

A JSON boolean used to go through Python ``str()`` and reach GitHub as
``"False"``, which a ``type: boolean`` or ``type: choice`` input refuses
("Provided value 'False' ... not in the list of allowed values").
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"


def _dispatch_tool():
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    fns = {c[0][0]: c[0][1] for c in executor.register.call_args_list}
    return fns["dispatch_github_workflow"]


class _GitHub:
    def __init__(self):
        self.payloads: list[dict] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method != "post":
            return httpx.Response(200, json={"workflow_runs": []}), None
        self.payloads.append(kwargs.get("json"))
        return httpx.Response(204), None


@pytest.mark.asyncio
async def test_booleans_and_numbers_are_sent_as_github_strings():
    github = _GitHub()
    with patch(_SAFE_REQUEST, new=github):
        answer = await _dispatch_tool()(
            repo="o/r", workflow_id="release.yml", ref="main", inputs={"flag": False, "n": 3}
        )
    assert "Dispatched" in answer
    assert github.payloads == [{"ref": "main", "inputs": {"flag": "false", "n": "3"}}]


@pytest.mark.asyncio
async def test_true_strings_null_and_objects():
    github = _GitHub()
    inputs = {"on": True, "name": "x", "none": None, "obj": {"a": 1}, "ratio": 1.5}
    with patch(_SAFE_REQUEST, new=github):
        await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main", inputs=inputs)
    assert github.payloads[0]["inputs"] == {"on": "true", "name": "x", "none": "", "obj": '{"a": 1}', "ratio": "1.5"}


@pytest.mark.asyncio
async def test_no_inputs_sends_no_inputs_key():
    github = _GitHub()
    with patch(_SAFE_REQUEST, new=github):
        await _dispatch_tool()(repo="o/r", workflow_id="ci.yml", ref="main")
    assert github.payloads == [{"ref": "main"}]
