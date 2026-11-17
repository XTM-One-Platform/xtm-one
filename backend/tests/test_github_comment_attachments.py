"""Files of the run attached to a GitHub comment (add_github_comment, create_github_pr_review)
or pull request body (create_github_pr, update_github_pr): the committed name, the Markdown
lines, the refusals made before any write, the assets branch read or created, and the signed
commit."""

import base64
import hashlib
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.github import attachments, build_tools

_SAFE_REQUEST = "app.integrations.providers.github._common.safe_request"
API = "https://api.github.com/repos/acme/app"
GRAPHQL = "https://api.github.com/graphql"
TIP = "a" * 40
MAIN = "b" * 40
COMMIT = "c" * 40
PNG = b"\x89PNG\r\n\x1a\n\x00\xff"
ZIP = b"PK\x03\x04zip"


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def _file_context(files: dict[str, tuple[bytes, str, str]]) -> MagicMock:
    context = MagicMock()

    async def resolve_upload(file_id):
        return files.get(file_id)

    context.resolve_upload = resolve_upload
    return context


class FakeGitHub:
    def __init__(self, *, branch_exists: bool = True, graphql_errors: list | None = None) -> None:
        self.branch_exists = branch_exists
        self.graphql_errors = graphql_errors
        self.calls: list[tuple[str, str, dict]] = []

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _ in self.calls if method is None or verb == method]

    def payload(self, method: str, url: str) -> dict:
        return next(kw["json"] for verb, called, kw in self.calls if verb == method and called == url)

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "get" and url == f"{API}/git/ref/heads/{attachments.ASSETS_BRANCH}":
            if self.branch_exists:
                return httpx.Response(200, json={"object": {"sha": TIP}}), None
            return httpx.Response(404, json={"message": "Not Found"}), "Error: GitHub API error (404): Not Found"
        if method == "get" and url == API:
            return httpx.Response(200, json={"default_branch": "main"}), None
        if method == "get" and url == f"{API}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": MAIN}}), None
        if method == "post" and url == f"{API}/git/refs":
            return httpx.Response(201, json={"ref": "refs/heads/xtm-assets"}), None
        if method == "post" and url == GRAPHQL:
            if self.graphql_errors:
                return httpx.Response(200, json={"data": None, "errors": self.graphql_errors}), None
            return httpx.Response(200, json={"data": {"createCommitOnBranch": {"commit": {"oid": COMMIT}}}}), None
        if method == "post" and url.endswith("/comments"):
            return httpx.Response(201, json={"html_url": "https://github.com/acme/app/issues/7#c1"}), None
        if method == "post" and url.endswith("/reviews"):
            return httpx.Response(200, json={"html_url": "https://github.com/acme/app/pull/7#r1"}), None
        if method == "post" and url == f"{API}/pulls":
            pr = {"number": 9, "html_url": "https://github.com/acme/app/pull/9", "head": {"ref": "feat/x"}}
            return httpx.Response(201, json=pr), None
        if method == "get" and url == f"{API}/pulls/9":
            return httpx.Response(200, json={"number": 9, "body": "Current body"}), None
        if method == "patch" and url == f"{API}/pulls/9":
            return httpx.Response(200, json={"state": "open"}), None
        raise AssertionError(f"unexpected {method.upper()} {url}")


def _tool(name: str):
    executor = MagicMock()
    executor.register = MagicMock()
    definitions = build_tools("fake-token", executor)
    handler = next(call[0][1] for call in executor.register.call_args_list if call[0][0] == name)
    return handler, {d.name: d for d in definitions}


def test_the_committed_name_keeps_the_stem_behind_the_content_hash():
    assert attachments.attachment_name("Board 1 (final).PNG", "image/png", PNG) == (
        f"{_h(PNG)}-Board-1-final.png",
        ".png",
    )
    # No usable extension: the content type names it.
    assert attachments.attachment_name("capture", "image/webp", PNG) == (f"{_h(PNG)}-capture.webp", ".webp")
    assert attachments.attachment_name("run.exe", "application/x-msdownload", PNG) is None


def test_images_are_inline_and_other_files_linked_at_the_commit():
    lines = attachments.attachment_lines("acme/app", COMMIT, ["xtm-assets/7/ab-shot.png", "xtm-assets/7/cd-boards.zip"])
    assert lines == [
        f"![ab-shot.png](https://github.com/acme/app/raw/{COMMIT}/xtm-assets/7/ab-shot.png)",
        f"[cd-boards.zip](https://github.com/acme/app/blob/{COMMIT}/xtm-assets/7/cd-boards.zip)",
    ]


def test_the_body_keeps_its_text_and_gains_the_lines():
    assert attachments.compose_body("Verdict\n", ["![a](u)"]) == "Verdict\n\n![a](u)"
    assert attachments.compose_body("", ["![a](u)"]) == "![a](u)"
    assert attachments.compose_body("x", []) == "x"


def test_the_comment_and_pull_request_tools_declare_the_argument():
    _, definitions = _tool("add_github_comment")
    for name in ("add_github_comment", "create_github_pr_review", "create_github_pr", "update_github_pr"):
        assert definitions[name].input_schema["properties"]["attachment_file_ids"] == attachments.SCHEMA


@pytest.mark.asyncio
async def test_two_images_and_a_zip_land_in_one_signed_commit_and_the_comment():
    comment, _ = _tool("add_github_comment")
    fake = FakeGitHub()
    files = {
        "f1": (PNG, "board-1.png", "image/png"),
        "f2": (PNG + b"2", "board-2.png", "image/png"),
        "f3": (ZIP, "boards.zip", "application/zip"),
    }
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(
            repo="acme/app",
            issue_number=7,
            body="UX verdict",
            attachment_file_ids=["f1", "f2", "f3"],
            file_context=_file_context(files),
        )

    assert (
        result
        == "Comment added with 3 attachment(s) stored on branch xtm-assets: https://github.com/acme/app/issues/7#c1"
    )
    commit_input = fake.payload("post", GRAPHQL)["variables"]["input"]
    assert commit_input["branch"] == {"repositoryNameWithOwner": "acme/app", "branchName": "xtm-assets"}
    assert commit_input["expectedHeadOid"] == TIP
    added = {a["path"]: base64.b64decode(a["contents"]) for a in commit_input["fileChanges"]["additions"]}
    assert added == {
        f"xtm-assets/7/{_h(PNG)}-board-1.png": PNG,
        f"xtm-assets/7/{_h(PNG + b'2')}-board-2.png": PNG + b"2",
        f"xtm-assets/7/{_h(ZIP)}-boards.zip": ZIP,
    }
    body = fake.payload("post", f"{API}/issues/7/comments")["body"]
    assert body.startswith("UX verdict\n\n")
    assert f"![{_h(PNG)}-board-1.png](https://github.com/acme/app/raw/{COMMIT}/" in body
    assert f"[{_h(ZIP)}-boards.zip](https://github.com/acme/app/blob/{COMMIT}/" in body
    assert f"{API}/git/refs" not in fake.urls("post")


@pytest.mark.asyncio
async def test_a_missing_assets_branch_is_created_from_the_default_branch_never_written_there():
    comment, _ = _tool("add_github_comment")
    fake = FakeGitHub(branch_exists=False)
    with patch(_SAFE_REQUEST, new=fake):
        await comment(
            repo="acme/app",
            issue_number=7,
            body="x",
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "a.png", "image/png")}),
        )

    assert fake.payload("post", f"{API}/git/refs") == {"ref": "refs/heads/xtm-assets", "sha": MAIN}
    commit_input = fake.payload("post", GRAPHQL)["variables"]["input"]
    assert commit_input["branch"]["branchName"] == "xtm-assets"
    assert commit_input["expectedHeadOid"] == MAIN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "files", "expected"),
    [
        (["f9"], {}, "was not found in this conversation"),
        (["f1"], {"f1": (PNG, "run.exe", "application/x-msdownload")}, "is not a type a comment takes"),
        ([f"f{i}" for i in range(11)], {}, "at most 10 attachments"),
        (["f1"], {"f1": (b"x" * (attachments.MAX_ATTACHMENT_BYTES + 1), "a.png", "image/png")}, "upload limit"),
    ],
)
async def test_a_refused_attachment_posts_nothing(ids, files, expected):
    comment, _ = _tool("add_github_comment")
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(
            repo="acme/app", issue_number=7, body="x", attachment_file_ids=ids, file_context=_file_context(files)
        )

    assert expected in result and "Nothing was posted." in result
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_refused_commit_posts_no_comment():
    comment, _ = _tool("add_github_comment")
    fake = FakeGitHub(graphql_errors=[{"message": "Branch is protected"}])
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(
            repo="acme/app",
            issue_number=7,
            body="x",
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "a.png", "image/png")}),
        )

    assert "Branch is protected" in result and "Nothing was posted." in result
    assert not any(url.endswith("/comments") for url in fake.urls("post"))


@pytest.mark.asyncio
async def test_a_comment_without_attachments_is_unchanged():
    comment, _ = _tool("add_github_comment")
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await comment(repo="acme/app", issue_number=7, body="plain")

    assert result == "Comment added: https://github.com/acme/app/issues/7#c1"
    assert fake.urls() == [f"{API}/issues/7/comments"]
    assert fake.payload("post", f"{API}/issues/7/comments") == {"body": "plain"}


@pytest.mark.asyncio
async def test_a_review_carries_its_attachments():
    review, _ = _tool("create_github_pr_review")
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await review(
            repo="acme/app",
            pr_number=7,
            event="COMMENT",
            body="see the capture",
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "shot.png", "image/png")}),
        )

    assert result.startswith("Review submitted (COMMENT)")
    body = fake.payload("post", f"{API}/pulls/7/reviews")["body"]
    assert body.startswith("see the capture\n\n![")


def _pr_checks_passed(stack: ExitStack) -> None:
    """The title, closing-keyword, overlap and full-CI reads of create_github_pr, out of the way."""
    base = "app.integrations.providers.github."
    stack.enter_context(patch(base + "pr_title.checked_title", new=AsyncMock(return_value=(None, None))))
    stack.enter_context(patch(base + "closing_refs.pr_closing_refusal", new=AsyncMock(return_value=None)))
    stack.enter_context(patch(base + "pr_overlap.overlap_warning", new=AsyncMock(return_value=None)))
    stack.enter_context(patch(base + "full_ci_gate.start_on_push", new=AsyncMock(return_value=None)))


@pytest.mark.asyncio
async def test_a_new_pull_request_body_shows_the_image_stored_outside_its_branch():
    create, _ = _tool("create_github_pr")
    fake = FakeGitHub()
    with ExitStack() as stack:
        stack.enter_context(patch(_SAFE_REQUEST, new=fake))
        _pr_checks_passed(stack)
        result = await create(
            repo="acme/app",
            title="fix(x): y (#1)",
            head="feat/x",
            base="main",
            body="Before / after",
            draft=True,
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "after.png", "image/png")}),
        )

    assert result.startswith("PR #9 created: https://github.com/acme/app/pull/9")
    assert "1 attachment(s) stored on branch xtm-assets" in result
    commit_input = fake.payload("post", GRAPHQL)["variables"]["input"]
    # Only the assets branch is written: never the pull request's head.
    assert commit_input["branch"]["branchName"] == "xtm-assets"
    added = [a["path"] for a in commit_input["fileChanges"]["additions"]]
    assert added == [f"xtm-assets/pr-feat-x/{_h(PNG)}-after.png"]
    body = fake.payload("post", f"{API}/pulls")["body"]
    assert body == (
        f"Before / after\n\n![{_h(PNG)}-after.png](https://github.com/acme/app/raw/{COMMIT}/"
        f"xtm-assets/pr-feat-x/{_h(PNG)}-after.png)"
    )
    assert fake.payload("post", f"{API}/pulls")["draft"] is True


@pytest.mark.asyncio
async def test_a_refused_image_opens_no_pull_request():
    create, _ = _tool("create_github_pr")
    fake = FakeGitHub()
    big = b"x" * (attachments.MAX_ATTACHMENT_BYTES + 1)
    with ExitStack() as stack:
        stack.enter_context(patch(_SAFE_REQUEST, new=fake))
        _pr_checks_passed(stack)
        result = await create(
            repo="acme/app",
            title="fix(x): y (#1)",
            head="feat/x",
            base="main",
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (big, "a.png", "image/png")}),
        )

    assert "upload limit" in result and "Nothing was posted." in result
    assert fake.calls == []


@pytest.mark.asyncio
async def test_an_update_appends_the_image_to_the_current_body():
    update, _ = _tool("update_github_pr")
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await update(
            repo="acme/app",
            number=9,
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "shot.png", "image/png")}),
        )

    assert result == "PR #9 updated [open] in acme/app.\n1 attachment(s) stored on branch xtm-assets"
    added = [a["path"] for a in fake.payload("post", GRAPHQL)["variables"]["input"]["fileChanges"]["additions"]]
    assert added == [f"xtm-assets/9/{_h(PNG)}-shot.png"]
    body = fake.payload("patch", f"{API}/pulls/9")["body"]
    assert body.startswith("Current body\n\n![") and f"/raw/{COMMIT}/xtm-assets/9/" in body


@pytest.mark.asyncio
async def test_an_update_with_a_wrong_type_writes_nothing():
    update, _ = _tool("update_github_pr")
    fake = FakeGitHub()
    with patch(_SAFE_REQUEST, new=fake):
        result = await update(
            repo="acme/app",
            number=9,
            body="New body",
            attachment_file_ids=["f1"],
            file_context=_file_context({"f1": (PNG, "run.exe", "application/x-msdownload")}),
        )

    assert "is not a type a comment takes" in result and "Nothing was posted." in result
    assert not fake.urls("patch") and not fake.urls("post")
