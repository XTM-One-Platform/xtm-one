"""manage_github_release: only the given fields are sent, every write is read back.

A draft created on tag X, updated (body only), then published must stay on tag X:
no request carries an empty tag_name, publish sends the draft's tag explicitly
and refuses a draft left on GitHub's ``untagged-*`` placeholder, and a
target_commitish given on update is sent and read back (#10684).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import build_tools

_REL = "https://api.github.com/repos/o/r/releases"


def _fn(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor)
    for call in executor.register.call_args_list:
        if call[0][0] == name:
            return call[0][1]
    raise KeyError(name)


class _FakeGitHub:
    """A release store answering safe_request like the GitHub releases API."""

    def __init__(self, *, ignore_target: bool = False) -> None:
        self.release: dict = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.ignore_target = ignore_target

    async def __call__(self, client, method, url, provider, label, **kwargs):
        body = kwargs.get("json")
        self.calls.append((method, url, body))
        if method == "post" and url == _REL:
            self.release = {
                "id": 7,
                "tag_name": "untagged-abc" if body.get("draft") else body["tag_name"],
                "_real_tag": body["tag_name"],
                "target_commitish": body.get("target_commitish", "main"),
                "draft": bool(body.get("draft")),
                "prerelease": False,
                "name": body.get("name", ""),
                "html_url": "https://github.com/o/r/releases/tag/untagged-abc",
            }
            return httpx.Response(201, json=self._public()), None
        if method == "get" and url == f"{_REL}/latest":
            return httpx.Response(200, json={"id": self.release["id"]}), None
        if method == "get" and url == f"{_REL}/7":
            return httpx.Response(200, json=self._public()), None
        if method == "patch" and url == f"{_REL}/7":
            if "tag_name" in body:
                self.release["_real_tag"] = body["tag_name"]
            for key in ("name", "body", "prerelease", "draft"):
                if key in body:
                    self.release[key] = body[key]
            if "target_commitish" in body and not self.ignore_target:
                self.release["target_commitish"] = body["target_commitish"]
            tag = self.release["_real_tag"]
            self.release["tag_name"] = "untagged-abc" if self.release["draft"] else tag
            self.release["html_url"] = f"https://github.com/o/r/releases/tag/{self.release['tag_name']}"
            return httpx.Response(200, json=self._public()), None
        raise AssertionError(f"unexpected {method} {url}")

    def _public(self) -> dict:
        # A draft's REST tag_name is the real tag; only the URL carries untagged-*.
        out = {k: v for k, v in self.release.items() if not k.startswith("_")}
        out["tag_name"] = self.release["_real_tag"] if self.release.get("_real_tag") else out["tag_name"]
        return out


@pytest.mark.asyncio
async def test_create_update_body_publish_keeps_the_tag():
    fn = _fn("manage_github_release")
    gh = _FakeGitHub()
    with patch("app.integrations.providers.github._common.safe_request", new=AsyncMock(side_effect=gh.__call__)):
        created = await fn(repo="o/r", action="create", tag_name="1.2.3", draft=True, target_commitish="a" * 40)
        updated = await fn(repo="o/r", action="update", release_id=7, body="notes")
        published = await fn(repo="o/r", action="publish", release_id=7)

    assert "created in o/r" in created and "tag_name: 1.2.3" in created and "draft: True" in created
    assert "updated in o/r" in updated and "NOT APPLIED" not in updated
    assert "published in o/r" in published
    assert "tag_name: 1.2.3" in published
    assert "draft: False" in published and "latest: true" in published
    bodies = [b for _, _, b in gh.calls if b is not None]
    assert all(b.get("tag_name", "x") != "" for b in bodies)
    update_body = next(b for m, _, b in gh.calls if m == "patch")
    assert update_body == {"body": "notes"}
    publish_body = [b for m, _, b in gh.calls if m == "patch"][1]
    assert publish_body == {"draft": False, "tag_name": "1.2.3"}


@pytest.mark.asyncio
async def test_update_target_commitish_is_sent_and_read_back():
    fn = _fn("manage_github_release")
    gh = _FakeGitHub()
    with patch("app.integrations.providers.github._common.safe_request", new=AsyncMock(side_effect=gh.__call__)):
        await fn(repo="o/r", action="create", tag_name="1.2.3", draft=True, target_commitish="a" * 40)
        out = await fn(repo="o/r", action="update", release_id=7, target_commitish="c" * 40)

    patch_body = next(b for m, _, b in gh.calls if m == "patch")
    assert patch_body == {"target_commitish": "c" * 40}
    assert f"target_commitish: {'c' * 40}" in out
    assert "NOT APPLIED" not in out


@pytest.mark.asyncio
async def test_update_target_commitish_not_applied_is_reported():
    fn = _fn("manage_github_release")
    gh = _FakeGitHub(ignore_target=True)
    with patch("app.integrations.providers.github._common.safe_request", new=AsyncMock(side_effect=gh.__call__)):
        await fn(repo="o/r", action="create", tag_name="1.2.3", draft=True, target_commitish="a" * 40)
        out = await fn(repo="o/r", action="update", release_id=7, target_commitish="c" * 40)

    assert "did not apply every field" in out
    assert "NOT APPLIED: target_commitish" in out


@pytest.mark.asyncio
async def test_publish_refuses_a_draft_on_an_untagged_placeholder():
    fn = _fn("manage_github_release")
    gh = _FakeGitHub()
    gh.release = {
        "id": 7,
        "tag_name": "untagged-2142c25181212ec0af55",
        "target_commitish": "main",
        "draft": True,
        "prerelease": False,
        "name": "",
        "html_url": "",
    }
    with patch("app.integrations.providers.github._common.safe_request", new=AsyncMock(side_effect=gh.__call__)):
        out = await fn(repo="o/r", action="publish", release_id=7)

    assert out.startswith("Error:")
    assert "untagged-2142c25181212ec0af55" in out
    assert not [c for c in gh.calls if c[0] == "patch"]


@pytest.mark.asyncio
async def test_publish_with_a_tag_fixes_a_placeholder_draft():
    fn = _fn("manage_github_release")
    gh = _FakeGitHub()
    gh.release = {
        "id": 7,
        "tag_name": "untagged-abc",
        "target_commitish": "main",
        "draft": True,
        "prerelease": False,
        "name": "",
        "html_url": "",
    }
    with patch("app.integrations.providers.github._common.safe_request", new=AsyncMock(side_effect=gh.__call__)):
        out = await fn(repo="o/r", action="publish", release_id=7, tag_name="2.0.0")

    assert [b for m, _, b in gh.calls if m == "patch"] == [{"draft": False, "tag_name": "2.0.0"}]
    assert "tag_name: 2.0.0" in out and "published in o/r" in out
