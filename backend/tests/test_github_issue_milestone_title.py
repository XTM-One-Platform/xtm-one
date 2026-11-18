"""An issue milestone named by its number or its exact title (create_github_issue, update_github_issue).

Both resolve it on the repository with :func:`pr_inherit.resolve_milestone`, as
``create_github_pr`` does: an unknown one refuses before any write.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"

_MILESTONES = [{"number": 7, "title": "1. Now"}, {"number": 8, "title": "2. Next"}]


def _tool(name: str) -> Any:
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}[name]


def _defs() -> dict[str, Any]:
    executor = MagicMock()
    executor.register = MagicMock()
    return {d.name: d for d in build_tools("fake-token", executor)}


class _GitHub:
    """Serves the milestones of one repository and records every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response, str | None]:
        if method in ("post", "patch"):
            self.writes.append((method, kwargs["json"]))
            return httpx.Response(200, json={"number": 5, "state": "open", "html_url": "h"}), None
        if "/contents/" in url:
            return httpx.Response(404, json={}), "not found"
        if url.endswith("/milestones"):
            return httpx.Response(200, json=_MILESTONES), None
        if "/milestones/" in url:
            number = int(url.rsplit("/", 1)[1])
            found = [m for m in _MILESTONES if m["number"] == number]
            if found:
                return httpx.Response(200, json=found[0]), None
            return httpx.Response(404, json={}), "not found"
        return httpx.Response(200, json={}), None


async def _no_self_action(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _quiet_self_action() -> Iterator[None]:
    with patch("app.integrations.providers.github.issues.mark_self_action", new=_no_self_action, create=True):
        yield


@pytest.mark.asyncio
async def test_create_resolves_a_milestone_title_to_its_number() -> None:
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("create_github_issue")(
            repo="o/r", title="feat(github): resolve an issue milestone by its title", milestone="1. now"
        )
    assert gh.writes == [("post", {"title": "feat(github): resolve an issue milestone by its title", "milestone": 7})]
    assert "in milestone '1. Now'" in out


@pytest.mark.asyncio
async def test_create_refuses_an_unknown_milestone_before_any_write() -> None:
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("create_github_issue")(repo="o/r", title="fix(x): y", milestone="3. Later")
    assert gh.writes == []
    assert out.startswith("Refused: milestone '3. Later' not found")
    assert "'1. Now'" in out


@pytest.mark.asyncio
async def test_update_takes_a_milestone_number_and_refuses_an_unknown_one() -> None:
    gh = _GitHub()
    with patch(_SAFE_REQUEST, new=gh):
        out = await _tool("update_github_issue")(repo="o/r", number=5, milestone=8)
        refused = await _tool("update_github_issue")(repo="o/r", number=5, milestone=99)
    assert gh.writes == [("patch", {"milestone": 8})]
    assert "in milestone '2. Next'" in out
    assert refused.startswith("Refused: milestone #99 not found")


def test_the_milestone_parameter_says_number_or_title() -> None:
    defs = _defs()
    for name in ("create_github_issue", "update_github_issue"):
        param = defs[name].input_schema["properties"]["milestone"]
        assert param["type"] == "string"
        assert "exact title" in param["description"]
