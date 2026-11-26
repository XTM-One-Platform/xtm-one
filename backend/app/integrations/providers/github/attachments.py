"""Files of the run attached to a GitHub comment or pull request body.

GitHub's REST API takes no upload for an issue or review comment: the
``user-attachments`` store the web editor uses is not offered to tokens. So a
file is committed to a dedicated branch of the repository (:data:`ASSETS_BRANCH`,
never the default branch) in ONE signed commit (``createCommitOnBranch``), and
the comment gains one Markdown line per file pointing at that commit: an
inline image for a picture, a link for anything else. A reader who can see the
repository sees the files; the links are pinned to the commit SHA, so a later
push on the branch never changes what the comment shows. A pull request body
(``create_github_pr``, ``update_github_pr``) takes them the same way: the
files never enter the pull request's own branch or diff.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
from typing import Any

from app.integrations.file_delivery import read_pipe_file

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_str, _enc, _json_headers, parse_repo
from .contents import _CREATE_COMMIT_MUTATION, _GIT_OBJECT_ID_RE

#: The branch the files are committed to: created from the default branch's tip when missing.
ASSETS_BRANCH = "xtm-assets"
#: Files one comment may carry, and their total size (one ``createCommitOnBranch`` stays under GitHub's cap).
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
#: Extensions shown inline as an image.
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
#: Extensions linked from the comment.
LINK_EXTENSIONS = frozenset({".pdf", ".zip", ".txt", ".csv", ".json", ".md", ".log", ".html", ".svg"})
#: The content types a file with no usable extension is named by.
_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/markdown": ".md",
}
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
#: Attempts of the attachments commit when another commit moved the branch tip meanwhile.
ASSETS_COMMIT_ATTEMPTS = 3
#: GitHub's refusal of ``createCommitOnBranch`` when ``expectedHeadOid`` is no longer the tip.
_TIP_MOVED = re.compile(r"expected branch to point to|expectedHeadOid|is at .* but expected", re.IGNORECASE)


def tip_moved(err: str | None) -> bool:
    """Whether *err* is the refusal of a commit whose expected tip another commit moved. Pure."""
    return bool(err) and bool(_TIP_MOVED.search(err or ""))


#: The ``attachment_file_ids`` argument of a comment tool.
SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Optional: file ids of this conversation (png/jpg/gif/webp inline; pdf/zip/txt/csv/json/md linked), max 10, 5 MB",
}


def attachment_name(filename: str, content_type: str, data: bytes) -> tuple[str, str] | None:
    """``(file name, extension)`` of a file as it is committed, ``None`` for a type a comment does not take.

    The name keeps the file's own stem (characters outside ``[A-Za-z0-9._-]``
    become ``-``) behind the first 8 hex of its sha256, so two files of the
    same name never overwrite each other and a re-attached file is unchanged.
    """
    stem, ext = posixpath.splitext(posixpath.basename(_coerce_str(filename).strip()))
    ext = ext.lower()
    if ext not in IMAGE_EXTENSIONS and ext not in LINK_EXTENSIONS:
        ext = _TYPE_EXTENSIONS.get(_coerce_str(content_type).split(";")[0].strip().lower(), "")
        stem = stem or "file"
    if not ext:
        return None
    safe = _UNSAFE_NAME.sub("-", stem).strip("-.")[:60] or "file"
    return f"{hashlib.sha256(data).hexdigest()[:8]}-{safe}{ext}", ext


def attachment_lines(repo: str, commit: str, paths: list[str]) -> list[str]:
    """The Markdown line of each committed file: an inline image, or a link, pinned to *commit*."""
    lines = []
    for path in paths:
        name = posixpath.basename(path)
        url_path = _enc(path)
        if posixpath.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
            lines.append(f"![{name}](https://github.com/{repo}/raw/{commit}/{url_path})")
        else:
            lines.append(f"[{name}](https://github.com/{repo}/blob/{commit}/{url_path})")
    return lines


def compose_body(body: str, lines: list[str]) -> str:
    """*body* followed by the attachment lines, one blank line between."""
    if not lines:
        return body
    head = (body or "").rstrip()
    return f"{head}\n\n" + "\n".join(lines) if head else "\n".join(lines)


async def _assets_tip(client: Any, headers: dict[str, str], owner: str, name: str) -> tuple[str, str | None]:
    """The tip of :data:`ASSETS_BRANCH`, created from the default branch's tip when missing."""
    repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/git/ref/heads/{ASSETS_BRANCH}",
        _PROVIDER,
        f"read branch {ASSETS_BRANCH} of {owner}/{name}",
        headers=headers,
        expected_statuses={404},
    )
    if err is None and resp is not None:
        sha = _coerce_str(((resp.json() or {}).get("object") or {}).get("sha"))
        if _GIT_OBJECT_ID_RE.fullmatch(sha):
            return sha, None
    elif resp is None or resp.status_code != 404:
        return "", err
    resp, err = await _common.safe_request(client, "get", repo_url, _PROVIDER, f"read {owner}/{name}", headers=headers)
    if err:
        return "", err
    default = _coerce_str((resp.json() or {}).get("default_branch")) or "main"
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/git/ref/heads/{_enc(default)}",
        _PROVIDER,
        f"read branch {default} of {owner}/{name}",
        headers=headers,
    )
    if err:
        return "", err
    base = _coerce_str(((resp.json() or {}).get("object") or {}).get("sha"))
    resp, err = await _common.safe_request(
        client,
        "post",
        f"{repo_url}/git/refs",
        _PROVIDER,
        f"create branch {ASSETS_BRANCH} of {owner}/{name}",
        headers=_json_headers(headers),
        json={"ref": f"refs/heads/{ASSETS_BRANCH}", "sha": base},
    )
    if err:
        return "", err
    return base, None


async def attach_files(
    client: Any,
    headers: dict[str, str],
    repo: str,
    number: int,
    file_ids: list[str] | None,
    file_context: Any,
    folder: str = "",
) -> tuple[list[str], str | None]:
    """Commit the files *file_ids* names to :data:`ASSETS_BRANCH` and answer their Markdown lines.

    The files go under ``xtm-assets/<number>/``, or ``xtm-assets/<folder>/``
    when *folder* is given (a pull request not opened yet has no number: its
    head branch names the folder). Every file is read and checked before
    anything is written: a missing file, a type a comment does not take or a
    size over the cap refuses the whole call. ``([], None)`` when there is
    nothing to attach.
    """
    ids = [i for i in (_coerce_str(x).strip() for x in (file_ids or [])) if i]
    if not ids:
        return [], None
    if len(ids) > MAX_ATTACHMENTS:
        return [], f"Error: at most {MAX_ATTACHMENTS} attachments per comment ({len(ids)} given). Nothing was posted."
    owner, name, rerr = parse_repo(repo)
    if rerr:
        return [], rerr
    if folder:
        place = _UNSAFE_NAME.sub("-", _coerce_str(folder)).strip("-.")[:80] or "pr"
        subject = place
    else:
        place = str(int(number))
        subject = f"#{place}"
    additions: dict[str, bytes] = {}
    total = 0
    for file_id in ids:
        piped = await read_pipe_file(file_context, file_id, max_bytes=MAX_ATTACHMENT_BYTES, target="a GitHub comment")
        if isinstance(piped, str):
            return [], f"{piped} Nothing was posted."
        content, filename, content_type = piped
        named = attachment_name(filename, content_type, content)
        if named is None:
            allowed = ", ".join(sorted(IMAGE_EXTENSIONS | LINK_EXTENSIONS))
            return [], (
                f"Error: file '{file_id}' is not a type a comment takes (allowed: {allowed}). Nothing was posted."
            )
        total += len(content)
        additions[f"{ASSETS_BRANCH}/{place}/{named[0]}"] = content
    if total > MAX_ATTACHMENT_BYTES:
        return [], (
            f"Error: the attachments total {round(total / 1024 / 1024, 1)} MB, over the "
            f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB of one comment. Nothing was posted."
        )
    file_changes = {
        "additions": [
            {"path": path, "contents": base64.b64encode(data).decode("ascii")} for path, data in additions.items()
        ]
    }
    oid, err = "", None
    # Parallel comments race on the branch tip (gap comment-attachments-xtm-assets-race):
    # a commit refused because another one moved the tip is retried on the new tip.
    # ``expectedHeadOid`` keeps a retry from landing twice: a first attempt that
    # landed moved the tip, and its files (named by their content hash) are the same.
    for _attempt in range(ASSETS_COMMIT_ATTEMPTS):
        tip, err = await _assets_tip(client, headers, owner, name)
        if err:
            return [], f"{err} The attachments were not stored; nothing was posted."
        commit_input = {
            "branch": {"repositoryNameWithOwner": f"{owner}/{name}", "branchName": ASSETS_BRANCH},
            "expectedHeadOid": tip,
            "message": {"headline": f"chore(assets): attachments of {subject}"},
            "fileChanges": file_changes,
        }
        answer, err = await _common.gh_graphql(
            client,
            headers,
            _CREATE_COMMIT_MUTATION,
            {"input": commit_input},
            action=f"commit the attachments of {subject} to {ASSETS_BRANCH} of {repo}",
        )
        created = answer.get("createCommitOnBranch") if isinstance(answer, dict) else None
        commit = created.get("commit") if isinstance(created, dict) else None
        oid = _coerce_str(commit.get("oid")) if isinstance(commit, dict) else ""
        if _GIT_OBJECT_ID_RE.fullmatch(oid) or not tip_moved(err):
            break
    if err or not _GIT_OBJECT_ID_RE.fullmatch(oid):
        why = err or f"Error: GitHub did not return the commit of the attachments on {ASSETS_BRANCH}."
        return [], f"{why} Nothing was posted."
    return attachment_lines(repo, oid, list(additions)), None
