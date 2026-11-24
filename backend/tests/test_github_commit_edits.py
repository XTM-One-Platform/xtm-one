"""create_github_commit files[].edits: find/replace applied server-side to
the file at the branch tip, all or nothing, in the one signed commit, with the
diff read back; no sandbox involved."""

import base64
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.github import contents
from tests.test_github_commit import (
    _SAFE_REQUEST,
    API,
    GRAPHQL,
    NEW_COMMIT,
    TIP,
    FakeGitHub,
    _commit_tool,
    _committed_paths,
    _CountingGitHub,
)

#: The text every edited file has on the branch unless a test says otherwise.
_README = "# App\n\nversion: \"0.4.3\"\nmore: text\n"


class EditingGitHub(FakeGitHub):
    """The commit fake, also answering a blob read with *text*."""

    def __init__(self, text: str = _README, **kwargs) -> None:
        super().__init__(**kwargs)
        self.text = text

    async def __call__(self, client, method, url, provider, action, **kwargs):
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            self.calls.append((method, url, kwargs))
            encoded = base64.b64encode(self.text.encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"content": encoded, "encoding": "base64"}), None
        return await super().__call__(client, method, url, provider, action, **kwargs)


def _additions(fake: FakeGitHub) -> dict[str, str]:
    changes = fake.payload("post", GRAPHQL)["variables"]["input"]["fileChanges"]
    return {a["path"]: base64.b64decode(a["contents"]).decode("utf-8") for a in changes.get("additions", [])}


@pytest.mark.asyncio
async def test_one_line_is_changed_in_one_signed_commit_and_the_diff_comes_back():
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore(app): bump",
            files=[
                {
                    "path": "README.md",
                    "edits": [{"find": 'version: "0.4.3"', "replace": 'version: "0.4.3" # pinned'}],
                }
            ],
        )

    assert _additions(fake) == {"README.md": '# App\n\nversion: "0.4.3" # pinned\nmore: text\n'}
    assert fake.payload("post", GRAPHQL)["variables"]["input"]["expectedHeadOid"] == TIP
    assert f"({NEW_COMMIT})" in result
    assert "1 updated" in result
    assert "GitHub built and signed this commit itself" in result
    assert "Edits applied:" in result
    assert '-version: "0.4.3"' in result
    assert '+version: "0.4.3" # pinned' in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("find", "expected"),
    [
        ("absent text", "'find' text not found ('absent text')"),
        ("e", "occurs"),
    ],
)
async def test_a_find_absent_or_not_unique_refuses_the_commit(find, expected):
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore: x",
            files=[{"path": "README.md", "edits": [{"find": find, "replace": "y"}]}],
        )

    assert result.startswith("Error: 'README.md' on branch 'feat/x' of acme/app: edit 1:")
    assert expected in result
    assert "The branch is unchanged." in result
    assert "get_github_file" in result or "occurs" in result
    # Nothing was built, committed or moved.
    assert fake.urls("post") == []
    assert fake.urls("patch") == []


@pytest.mark.asyncio
async def test_replace_all_changes_every_occurrence():
    commit, _ = _commit_tool()
    fake = EditingGitHub(text="a a a\n")
    with patch(_SAFE_REQUEST, new=fake):
        await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore: x",
            files=[{"path": "README.md", "edits": [{"find": "a", "replace": "b", "replace_all": True}]}],
        )

    assert _additions(fake) == {"README.md": "b b b\n"}


@pytest.mark.asyncio
async def test_edits_mix_with_full_contents_and_deletions_in_one_commit():
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="feat: several",
            files=[
                {"path": "README.md", "edits": [{"find": "more: text", "replace": "more: words"}]},
                {"path": "src/new.py", "content": "print('new')\n"},
            ],
            delete_paths=["src/old.py"],
        )

    assert _additions(fake) == {
        "README.md": '# App\n\nversion: "0.4.3"\nmore: words\n',
        "src/new.py": "print('new')\n",
    }
    assert fake.payload("post", GRAPHQL)["variables"]["input"]["fileChanges"]["deletions"] == [{"path": "src/old.py"}]
    assert "1 added, 1 updated, 1 deleted" in result


@pytest.mark.asyncio
async def test_a_crlf_file_keeps_its_endings():
    commit, _ = _commit_tool()
    fake = EditingGitHub(text="one\r\ntwo\r\n")
    with patch(_SAFE_REQUEST, new=fake):
        await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore: x",
            files=[{"path": "README.md", "edits": [{"find": "two\n", "replace": "three\n"}]}],
        )

    assert _additions(fake) == {"README.md": "one\r\nthree\r\n"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"path": "missing.md", "edits": [{"find": "a", "replace": "b"}]}, "does not exist on branch"),
        ({"path": "src", "edits": [{"find": "a", "replace": "b"}]}, "is a directory"),
        ({"path": "link", "edits": [{"find": "a", "replace": "b"}]}, "symbolic link"),
    ],
)
async def test_only_an_existing_file_can_be_edited(entry, expected):
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await commit(repo="acme/app", branch="feat/x", message="chore: x", files=[entry])

    assert expected in result
    assert fake.urls("post") == []


@pytest.mark.asyncio
async def test_a_file_that_is_not_utf8_text_is_refused():
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    fake.text = ""

    async def binary(client, method, url, provider, action, **kwargs):
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            encoded = base64.b64encode(b"\x89PNG\xff\x00").decode("ascii")
            return httpx.Response(200, json={"content": encoded, "encoding": "base64"}), None
        return await fake(client, method, url, provider, action, **kwargs)

    with patch(_SAFE_REQUEST, new=binary):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore: x",
            files=[{"path": "README.md", "edits": [{"find": "a", "replace": "b"}]}],
        )

    assert "is not UTF-8 text" in result
    assert fake.urls("post") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"path": "README.md", "edits": []}, "non-empty list"),
        ({"path": "README.md", "edits": [{"find": "", "replace": "b"}]}, "'find' must be a non-empty string"),
        (
            {"path": "README.md", "content": "x", "edits": [{"find": "a", "replace": "b"}]},
            "gives 'edits' and also 'content' or 'file_id'",
        ),
    ],
)
async def test_malformed_edits_are_refused_before_any_request(entry, expected):
    commit, _ = _commit_tool()
    fake = EditingGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await commit(repo="acme/app", branch="feat/x", message="chore: x", files=[entry])

    assert expected in result
    assert fake.calls == []


class _SplitEditingGitHub(_CountingGitHub):
    """The split fake, also answering a blob read with *text*."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    async def __call__(self, client: Any, method: str, url: str, provider: Any, action: str, **kwargs: Any) -> Any:
        if method == "get" and url.startswith(f"{API}/git/blobs/"):
            self.calls.append((method, url, kwargs))
            encoded = base64.b64encode(self.text.encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"content": encoded, "encoding": "base64"}), None
        return await super().__call__(client, method, url, provider, action, **kwargs)  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_edits_whose_resolved_files_pass_the_cap_land_as_consecutive_signed_commits() -> None:
    """One-line edits on several large files: the cap counts whole files, so
    the set goes through the split path instead of being refused (#1307)."""
    commit, _ = _commit_tool()
    fake = _SplitEditingGitHub(text="a" * 6 + "\n")
    edit = [{"find": "a\n", "replace": "b\n"}]
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "_MAX_COMMIT_CONTENT_BYTES", 10):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="feat(app): x (#1)",
            files=[{"path": "README.md", "edits": edit}, {"path": "AGENTS.md", "edits": edit}],
        )

    assert isinstance(result, str) and result.startswith("Committed the change set to branch 'feat/x'"), result
    assert sorted(_committed_paths(c)[0] for c in fake.commits) == ["AGENTS.md", "README.md"]
    assert all(c["expectedHeadOid"] for c in fake.commits)
    assert "Edits applied:" in result
    assert "+aaaaab" in result
    # The target moved once, to the last part.
    assert len(fake.urls("patch")) == 1


@pytest.mark.asyncio
async def test_edits_with_one_resolved_file_over_the_cap_are_still_refused() -> None:
    commit, _ = _commit_tool()
    fake = _SplitEditingGitHub(text="a" * 12 + "\n")
    with patch(_SAFE_REQUEST, new=fake), patch.object(contents, "_MAX_COMMIT_CONTENT_BYTES", 10):
        result = await commit(
            repo="acme/app",
            branch="feat/x",
            message="chore: x",
            files=[{"path": "README.md", "edits": [{"find": "a\n", "replace": "b\n"}]}],
        )

    assert "over the 10 one signed commit may carry, so no split can commit it" in result
    assert fake.commits == []
    assert fake.urls("patch") == []


def test_the_tool_schema_documents_edits():
    _, definitions = _commit_tool()
    definition = next(d for d in definitions if d.name == "create_github_commit")
    item = definition.input_schema["properties"]["files"]["items"]["properties"]
    assert set(item["edits"]["items"]["required"]) == {"find", "replace"}
    assert "find/replace" in definition.description
