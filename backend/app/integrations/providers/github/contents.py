"""GitHub repository contents tools - read / download files, repo tree, create/update/delete
files, and commit several file changes at once."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import re
import secrets
import time
import weakref
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import (
    Link,
    PersistFileFn,
    deliver_file,
    link_lines,
    read_pipe_file,
)
from app.integrations.http import app_token_cache_key
from app.services.sandbox_workspace import editing
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
    current_workspace,
    register_fetched_file,
    workspace_ingest_requested,
)

from . import _common, full_ci_gate
from ._common import _BASE_URL, _GRAPHQL_URL, _PROVIDER, _coerce_int, _coerce_str, _enc, _json_headers, parse_repo
from .landed import commit_probe

logger = logging.getLogger(__name__)

_SOURCE = "integration:github"
# The contents API serves raw files up to 100 MB.
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# The contents API refuses a file body over ~100 MB; base64 inflates it by 4/3.
_MAX_COMMIT_BYTES = 50 * 1024 * 1024
# A whole repository archive is read in memory: bounded. A narrowed one is bounded by the workspace quota instead
# (_narrowed_archive_bound): its files are written to the workspace, so they fit the quota or are refused anyway.
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
# An archive narrowed to paths is streamed to a temporary file, never held whole: this bounds the disk it takes.
_MAX_STREAMED_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
# The files a narrowed archive keeps, uncompressed, at most: the workspace quota is the bound when it is lower.
_MAX_NARROWED_SELECTED_BYTES = 2 * 1024 * 1024 * 1024


def _narrowed_archive_bound() -> int:
    """The bytes a narrowed archive may keep, uncompressed and compressed: the sandbox workspace quota (capped by
    ``_MAX_NARROWED_SELECTED_BYTES``). A selection of files that are already compressed (images, seed data) is about
    as large compressed as on disk, so a fixed in-memory bound on the compressed archive refused a selection the
    workspace holds (materialize-archive-size-cap); ``extract_tree`` refuses past the quota left in any case."""
    from app.services.sandbox_workspace.manifest import Limits

    return min(_MAX_NARROWED_SELECTED_BYTES, max(_MAX_ARCHIVE_BYTES, Limits.from_settings().quota_bytes))


#: Characters of a file get_github_file returns per call by default, and at most.
_DEFAULT_FILE_CHARS = 50_000
_MAX_FILE_CHARS = 150_000


def _clamp_window(offset: object, max_chars: object) -> tuple[int, int]:
    start = max(0, _coerce_int(offset) or 0)
    size = _coerce_int(max_chars) or _DEFAULT_FILE_CHARS
    return start, max(1, min(size if size > 0 else _DEFAULT_FILE_CHARS, _MAX_FILE_CHARS))


def _file_window(path: str, branch: str, content: str, links: str, *, offset: int, max_chars: int) -> str:
    """The window of *content* the model asked for, with what remains counted."""
    total = len(content)
    if offset >= total and total:
        return f"File: {path} ({total:,} chars, branch: {branch}){links}\n\noffset {offset:,} is past the end of the file."
    chunk = content[offset : offset + max_chars]
    end = offset + len(chunk)
    header = f"File: {path} (size: {total:,} chars, branch: {branch})"
    if offset or end < total:
        header += f" - chars {offset:,}-{end:,}"
    tail = ""
    if end < total:
        tail = (
            f"\n\n[{total - end:,} more chars: continue with offset={end}, or pass to_workspace_path to read the "
            "whole file in the sandbox workspace]"
        )
    return f"{header}{links}\n\n{chunk}{tail}"


def _file_links(meta: dict[str, Any]) -> list[Link]:
    return [("Web URL", meta.get("html_url")), ("Download URL", meta.get("download_url"))]


def _file_links_text(meta: dict[str, Any]) -> str:
    """The file's links, one per line, each preceded by a line break."""
    return "".join(f"\n{line}" for line in link_lines(_file_links(meta)))
# How long to cache a 404 "file not found" response for get_github_file.
# Convention-file probes (AGENTS.md, .github/copilot-instructions.md, …) that
# target repositories without those files generate repeated WARN log noise and
# waste tool-call round-trips on every assignment run.  Caching 404s for this
# TTL keeps subsequent runs silent while still picking up a newly-created file
# after the cache expires.
_FILE_404_CACHE_TTL = 24 * 3600  # 24 hours

#: Only 404s for these well-known convention/config files are negative-cached.
#: Restricting the cache to a fixed allow-list keeps get_github_file's
#: semantics unchanged for arbitrary reads (a freshly-pushed source file is
#: never masked by a stale cached miss) and bounds Redis key growth to a
#: handful of keys per credential/repo/branch — an agent probing arbitrary
#: paths cannot mint unbounded keys. Absence of these files is stable enough
#: that a 24 h TTL is safe; tool-driven creation is invalidated explicitly by
#: create_or_update_github_file.
#:
#: Matching is CASE-SENSITIVE against the canonical spellings below: git
#: paths are case-sensitive (a repo can contain ``agents.md`` but not
#: ``AGENTS.md``), so lower-casing either the match or the cache key would
#: let a cached miss for one casing shadow an existing file with another —
#: and a case-insensitive match with a case-preserving key would let an
#: agent mint unbounded key variants (``AgEnTs.Md``, …). Exact canonical
#: names are what convention probes actually request.
_NEGATIVE_CACHE_PATHS: frozenset[str] = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        ".cursorrules",
        ".github/copilot-instructions.md",
        ".github/labels.yml",
        ".github/LABELS.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE.md",
    }
)

#: Cache-key branch token used when the caller omits ``branch``. Checking the
#: cache under this token *before* default-branch resolution lets a cached
#: miss short-circuit with zero HTTP calls (the repo-metadata lookup included).
#: The token contains ``@{``, which ``git check-ref-format`` forbids in ref
#: names, so it can never equal a real branch — and explicit branches are
#: additionally namespaced with a ``ref:`` prefix by :func:`_branch_cache_token`,
#: so even a pathological explicit request for this exact string cannot share
#: the omitted-branch key.
_OMITTED_BRANCH_SENTINEL = "@{default}"


def _branch_cache_token(requested_branch: str) -> str:
    """Unambiguous cache-key token for the branch *as requested*.

    Explicit branches are prefixed with ``ref:`` and the omitted case maps to
    :data:`_OMITTED_BRANCH_SENTINEL`; the two namespaces cannot collide for any
    input, so a cached miss for an explicitly-requested branch can never be
    served to (or invalidated as) an omitted-branch lookup, and vice versa.
    """
    return f"ref:{requested_branch}" if requested_branch else _OMITTED_BRANCH_SENTINEL


def _is_negative_cacheable(path: str) -> bool:
    """True when a 404 for *path* may be served from / stored in the cache."""
    return path in _NEGATIVE_CACHE_PATHS


def _token_digest(headers: dict[str, str]) -> str:
    """Secret-free digest of the request credential, for cache-key scoping.

    GitHub's contents API answers 404 both for a genuinely missing file and
    for a private repository the token cannot see, so a cached miss is only
    valid for the credential that observed it — an unscoped key would let one
    integration's access-denied 404 shadow a file that another integration's
    token can read. Hashing via :func:`app_token_cache_key` keeps the token
    itself out of Redis; 16 hex chars (64 bits) is ample for key dispersion.
    """
    return app_token_cache_key(_PROVIDER, headers.get("Authorization", ""))[:16]


def _file_404_cache_key(token_digest: str, owner: str, name: str, branch: str, path: str) -> str:
    """Redis key for a cached 404 on a specific file.

    Scoped per credential digest (see :func:`_token_digest`) and
    case-insensitive on owner/name (GitHub repo identifiers are
    case-insensitive); branch and path stay case-sensitive as in git.
    """
    return f"github:file_404:{token_digest}:{owner.lower()}/{name.lower()}:{branch}:{path}"


async def _get_redis() -> Any | None:
    """Return the shared Redis client, or ``None`` when Redis is unavailable.

    The import is deferred so this module keeps importing (and the GitHub
    tools keep working) in environments without the redis package; any
    initialisation error degrades to "no cache" — the 404 cache always fails
    open to the plain HTTP path.
    """
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception as exc:
        logger.debug("GitHub file-404 cache unavailable (Redis down?): %s", format_exception_message(exc))
        return None


# Conventional-commit subject validation for GitHub content writes. The scope
# charset deliberately mirrors the strictest commit-message ruleset observed in
# the wild (`(\([a-zA-Z0-9_\-\.]+\))?`) rather than "anything but ')'",
# so a subject with an exotic scope (spaces, '!', unicode) gets wrapped instead
# of being preserved and then rejected by the target repo's ruleset anyway.
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([A-Za-z0-9_.-]+\))?!?: .+"
)


async def _resolve_branch(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo: str,
    owner: str,
    name: str,
    branch: Any,
) -> tuple[str | None, str | None]:
    """Resolve an explicit/implicit branch or ref to a usable name.

    An explicit non-blank value is returned as-is; otherwise the repository's
    default branch is looked up. Like the incoming arg, a missing/null/blank
    ``default_branch`` falls back to ``"main"``. A 404 on the metadata lookup
    gets an actionable stale-config hint appended (see below).
    """
    resolved = _coerce_str(branch).strip()
    if resolved:
        return resolved, None
    # expected_statuses=set() opts into receiving the failed response (so the
    # 404 branch below can key off the real status code instead of matching
    # error-message text) while keeping every non-2xx in the warning log —
    # a repo-level 404 is a genuine config problem operators should see.
    repo_resp, repo_err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}",
        _PROVIDER,
        f"get default branch for {repo}",
        headers=headers,
        expected_statuses=set(),
    )
    if repo_err:
        # A 404 on the repo metadata endpoint means the repository does not
        # exist or the connected OAuth integration has lost access to it.
        # Append an actionable hint so the LLM (and the user reading the
        # assignment run log) can distinguish a stale config reference from
        # a transient failure and know what to do next. Other errors
        # (rate limit, 5xx, network) pass through unchanged.
        if repo_resp is not None and repo_resp.status_code == 404:
            repo_err = (
                f"{repo_err}\n"
                f"Repository '{repo}' cannot be accessed — it may have been "
                f"renamed, deleted, or moved to another organization. Verify "
                f"the repository exists and that the connected GitHub "
                f"integration still has access to it (the OAuth token may "
                f"need to be re-authorized if organization permissions have "
                f"changed)."
            )
        return None, repo_err
    resolved = _coerce_str(repo_resp.json().get("default_branch")).strip() or "main"
    return resolved, None


def _normalize_commit_message(message: Any, path: str, action: str) -> str:
    """Return a commit message whose subject is conventional-commit compliant.

    Commit-message rulesets validate the subject line, so only the
    subject is normalized: a compliant subject keeps the whole message (body
    included), a non-compliant one is wrapped as ``chore(github): ...``, and a
    blank/non-string message falls back to ``chore(github): <action> <path>``.
    """
    raw = _coerce_str(message).strip()
    if not raw:
        return f"chore(github): {action} {path}"
    subject, sep, body = raw.partition("\n")
    subject = subject.strip()
    if not _CONVENTIONAL_COMMIT_RE.match(subject):
        subject = f"chore(github): {subject}"
    return f"{subject}\n{body}" if sep else subject


async def _forget_cached_404s(headers: dict[str, str], owner: str, name: str, branch: str, paths: list[str]) -> None:
    """Drop this credential's cached 404 for the convention files just written.

    An immediate ``get_github_file`` re-read must not be served a stale "File
    not found" for up to ``_FILE_404_CACHE_TTL``. Both the explicit-branch key
    and the omitted-branch sentinel key are deleted (the miss may have been
    cached under either form). Other credentials' cached misses (if any) age
    out with the TTL.
    """
    cacheable = [path for path in paths if _is_negative_cacheable(path)]
    if not cacheable:
        return
    rc = await _get_redis()
    if rc is None:
        return
    digest = _token_digest(headers)
    keys = [
        key
        for path in cacheable
        for key in (
            _file_404_cache_key(digest, owner, name, _branch_cache_token(branch), path),
            _file_404_cache_key(digest, owner, name, _OMITTED_BRANCH_SENTINEL, path),
        )
    ]
    try:
        await rc.delete(*keys)
    except Exception as exc:
        logger.debug("get_github_file cache invalidation failed: %s", format_exception_message(exc))


# Bounds of one ``create_github_commit`` call. Its tree request carries every
# file's content inline, so a bigger change set is split into several commits.
_MAX_COMMIT_CHANGES = 100
_MAX_COMMIT_CONTENT_BYTES = 5 * 1024 * 1024
#: The most a ``from_checkout`` change set may weigh: over the cap of one
#: commit it is split into consecutive signed commits (``checkout_batches``),
#: made on a scratch branch, and the target branch moves once to the last.
_MAX_CHECKOUT_SPLIT_BYTES = 8 * _MAX_COMMIT_CONTENT_BYTES
#: The most changes a ``from_checkout`` change set may carry: over the
#: ``_MAX_COMMIT_CHANGES`` of one commit it is split the same way.
_MAX_CHECKOUT_SPLIT_CHANGES = 8 * _MAX_COMMIT_CHANGES
#: Where the split commits are built: ``tmp/commit-batch/<unix time>-<tip12>-<token>``.
#: The prefix says what it is (a temporary batch of one commit call, never a
#: product branch) and the creation time in the name lets the next split sweep
#: a leftover without reading its commit.
_SCRATCH_PREFIX = "tmp/commit-batch/"
#: A scratch branch older than this is a leftover (a run killed between its
#: creation and its deletion, or a refused move nobody took): the next split
#: commit on the same repository deletes it.
_SCRATCH_MAX_AGE_SECONDS = 24 * 3600
_SCRATCH_NAME_RE = re.compile(r"(\d{9,11})-[0-9a-f]{12}-[0-9a-f]{8}")
#: Leftovers one split deletes at most, so a sweep never delays a commit long.
_MAX_SCRATCH_SWEEP = 20
#: Directories one commit may read to learn what its paths already are (a file
#: and its mode, a directory, a link, a submodule): one request each.
_MAX_COMMIT_TREE_READS = 60
_MAX_COMMIT_PATH_CHARS = 4096
#: Paths named per section of the ``create_github_commit`` result.
_MAX_LISTED_COMMIT_PATHS = 20

_MODE_FILE = "100644"
_MODE_EXECUTABLE = "100755"
_MODE_SYMLINK = "120000"

#: A git object id as the Git Data API returns it (SHA-1, or SHA-256 for a
#: repository in that format). Ids are interpolated into request paths.
_GIT_OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

_PLAIN_TEXT_ENCODINGS = frozenset({"", "utf-8", "utf8", "text"})

#: What ``git check-ref-format`` refuses anywhere in a ref name: control
#: characters, space, ``~ ^ : ? * [`` and a backslash.
_REF_FORBIDDEN_CHARS_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
#: Code points HFS+ ignores in a file name, so ``.g\u200cit`` is ``.git`` on a Mac.
_HFS_IGNORED_RE = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")


def _branch_name_error(branch: str) -> str | None:
    """Why *branch* is not a git branch name, or ``None`` when it is one.

    The name goes into the ref URLs with its ``/`` kept, and httpx resolves a
    ``.`` or ``..`` segment before sending: ``feature/../main`` would read and
    move ``main``, and enough ``..`` leave the repository. Git refuses every
    name refused here, so no branch that exists is.
    """
    if (
        ".." in branch
        or "@{" in branch
        or branch.endswith(".")
        or _REF_FORBIDDEN_CHARS_RE.search(branch)
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    ):
        return (
            f"Error: '{branch}' is not a valid branch name, so nothing was committed: git refuses '..', '@{{', "
            "spaces, control characters and ~ ^ : ? * [ \\ in a branch name, a trailing '.', and a '/'-separated "
            "part that is empty, starts with '.' or ends with '.lock'."
        )
    return None


def _names_dot_git(part: str) -> bool:
    """True when a path part is ``.git`` on some checkout.

    Besides any casing, Windows drops trailing dots and spaces and also knows
    ``.git`` as ``git~1`` or by a stream (``.git::$INDEX_ALLOCATION``), and
    macOS ignores some invisible code points. Git refuses all of them on
    checkout: an entry under one would be written into ``.git``.
    """
    name = _HFS_IGNORED_RE.sub("", part).rstrip(". ").lower()
    return name in (".git", "git~1") or name.startswith(".git:")


def _scratch_branch_name(tip: str, now: float) -> str:
    """The scratch branch a split commit is built on: its creation time, the tip it starts from, a token."""
    return f"{_SCRATCH_PREFIX}{int(now)}-{tip[:12]}-{secrets.token_hex(4)}"


def _stale_scratch_branches(names: list[str], now: float) -> list[str]:
    """The scratch branch names (prefix stripped or not) created over ``_SCRATCH_MAX_AGE_SECONDS`` before *now*.

    A name that does not carry a creation time is not ours and is never returned.
    """
    stale: list[str] = []
    for name in names:
        short = name.removeprefix("refs/heads/").removeprefix(_SCRATCH_PREFIX)
        match = _SCRATCH_NAME_RE.fullmatch(short)
        if match and now - int(match.group(1)) > _SCRATCH_MAX_AGE_SECONDS:
            stale.append(f"{_SCRATCH_PREFIX}{short}")
    return stale[:_MAX_SCRATCH_SWEEP]


async def _sweep_stale_scratch_branches(
    client: httpx.AsyncClient, headers: dict[str, str], repo_url: str, repo: str, now: float
) -> None:
    """Delete the scratch branches a previous split left on *repo* over a day ago. Best effort, never raises."""
    try:
        resp, err = await _common.safe_request(
            client, "get", f"{repo_url}/git/matching-refs/heads/{_SCRATCH_PREFIX.rstrip('/')}", _PROVIDER,
            f"list leftover scratch branches of {repo}", headers=headers,
        )
        if err or resp is None:
            return
        try:
            refs = resp.json()
        except ValueError:
            return
        if not isinstance(refs, list):
            return
        names = [str(ref.get("ref", "")) for ref in refs if isinstance(ref, dict)]
        for stale in _stale_scratch_branches(names, now):
            await _common.safe_request(
                client, "delete", f"{repo_url}/git/refs/heads/{_enc(stale)}", _PROVIDER,
                f"delete leftover scratch branch {stale} of {repo}", headers=headers,
            )
    except Exception as exc:  # a sweep never blocks the commit it precedes
        logger.warning("scratch branch sweep of %s failed: %s", repo, format_exception_message(exc))


def _json_object(resp: httpx.Response | None) -> dict[str, Any]:
    """The JSON object a response carries, or ``{}`` for anything else."""
    try:
        data = resp.json() if resp is not None else None
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(value: Any, field: str) -> tuple[list[Any], str | None]:
    """Read *value* as a list, accepting the JSON text of one.

    A model sometimes sends a nested array as its JSON string, and the
    tool-test endpoint bypasses the loop's schema coercion.
    """
    if value is None or value == "":
        return [], None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [], f"Error: '{field}' must be a list."
    if not isinstance(value, list):
        return [], f"Error: '{field}' must be a list."
    return value, None


def _commit_path(raw: Any, field: str) -> tuple[str, str | None]:
    """Normalize one path of a ``create_github_commit`` change set.

    Tree entries are resolved against the branch's tree, so a path is relative
    to the repository root: a leading ``/`` is dropped, and whitespace around
    it, an empty, ``.`` or ``..`` segment, a ``.git`` component (in any of its
    spellings, see :func:`_names_dot_git`), a backslash or a control character
    is refused rather than guessed at (' notes.md' and 'notes.md' are two files).
    """
    text = _coerce_str(raw)
    if not text.strip():
        return "", f"Error: every entry of '{field}' needs a non-empty path from the repository root."
    if text != text.strip():
        return "", f"Error: path {text!r} starts or ends with whitespace; give the path exactly as it is named."
    path = text.lstrip("/")
    if not path:
        return "", f"Error: every entry of '{field}' needs a non-empty path from the repository root."
    if len(path) > _MAX_COMMIT_PATH_CHARS:
        return "", f"Error: a path in '{field}' is longer than {_MAX_COMMIT_PATH_CHARS} characters."
    if "\\" in path:
        return "", f"Error: path '{path}' contains a backslash; separate directories with '/'."
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return "", f"Error: path {path!r} contains a control character."
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            return "", (
                f"Error: path '{path}' has an empty, '.' or '..' segment; give it from the repository root, "
                "e.g. 'src/app.py'."
            )
        if _names_dot_git(segment):
            return "", f"Error: path '{path}' is inside '.git', which a commit cannot write."
    return path, None


class _FileRef(NamedTuple):
    """A write whose content is a file of the run, read before any request."""

    file_id: str


class _WorkspaceBytes(NamedTuple):
    """A write whose content was read from the sandbox workspace (``from_checkout``)."""

    data: bytes


class _Edits(NamedTuple):
    """A write whose content is the file at the branch tip with these find/replace edits applied."""

    edits: list[editing.Edit]


def _parse_commit_changes(
    files: Any, delete_paths: Any
) -> tuple[list[tuple[str, str | _FileRef | _WorkspaceBytes | _Edits, bool | None]], list[str], str | None]:
    """Validate a change set: ``(writes, deletions, error)``.

    A write is ``(path, content, executable)``, ``executable`` being ``None``
    when the caller left the file's mode alone; ``content`` is plain text, a
    ``_FileRef`` for a file of the run given by ``file_id``, the
    ``_WorkspaceBytes`` ``from_checkout`` read from the sandbox workspace, or
    ``_Edits`` to apply to the file as it is at the branch tip.
    """
    raw_files, err = _json_list(files, "files")
    if err:
        return [], [], err
    raw_deletes, err = _json_list(delete_paths, "delete_paths")
    if err:
        return [], [], err
    if not raw_files and not raw_deletes:
        return [], [], "Error: nothing to commit - give 'files' to write and/or 'delete_paths' to remove."
    change_count = len(raw_files) + len(raw_deletes)
    if change_count > _MAX_COMMIT_CHANGES:
        return [], [], (
            f"Error: {change_count} changes exceed the {_MAX_COMMIT_CHANGES} one commit may carry; "
            "split them into several commits."
        )
    writes: list[tuple[str, str | _FileRef | _WorkspaceBytes | _Edits, bool | None]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in raw_files:
        file_id = _coerce_str(item.get("file_id")).strip() if isinstance(item, dict) else ""
        workspace = item.get("content") if isinstance(item, dict) and isinstance(item.get("content"), _WorkspaceBytes) else None
        has_content = isinstance(item, dict) and (isinstance(item.get("content"), str) or workspace is not None)
        raw_edits = item.get("edits") if isinstance(item, dict) else None
        if raw_edits is not None:
            # Find/replace on the file at the branch tip: the edits
            # are the whole source of the write, so nothing else is given.
            path, err = _commit_path(item.get("path"), "files")
            if err:
                return [], [], err
            if has_content or file_id:
                return [], [], (
                    f"Error: '{path}' gives 'edits' and also 'content' or 'file_id'; give one source per file "
                    "('edits' change the file as it is on the branch)."
                )
            try:
                edits = editing.parse_edits(raw_edits)
            except editing.EditError as exc:
                return [], [], f"Error: the edits of '{path}' are refused: {exc}. Nothing was committed."
            executable = item.get("executable")
            if executable is not None and not isinstance(executable, bool):
                return [], [], f"Error: 'executable' for '{path}' must be true or false."
            if path in seen:
                return [], [], f"Error: '{path}' is listed more than once."
            seen.add(path)
            writes.append((path, _Edits(edits), executable))
            continue
        if has_content == bool(file_id):
            return [], [], (
                "Error: each entry of 'files' needs a 'path' and either its full 'content' as plain text or a "
                "'file_id' (a file of this conversation), not both; or 'edits' alone (find/replace on the file "
                "as it is on the branch)."
            )
        path, err = _commit_path(item.get("path"), "files")
        if err:
            return [], [], err
        encoding = item.get("encoding")
        if has_content and encoding is not None and (
            not isinstance(encoding, str) or encoding.strip().lower() not in _PLAIN_TEXT_ENCODINGS
        ):
            return [], [], (
                f"Error: '{path}' is given as {encoding!r}; only plain-text content can be committed, or a file "
                "of this conversation by 'file_id'."
            )
        executable = item.get("executable")
        if executable is not None and not isinstance(executable, bool):
            return [], [], f"Error: 'executable' for '{path}' must be true or false."
        if path in seen:
            return [], [], f"Error: '{path}' is listed more than once."
        seen.add(path)
        if file_id:
            writes.append((path, _FileRef(file_id), executable))
            continue
        if workspace is not None:
            total_bytes += len(workspace.data)
            writes.append((path, workspace, executable))
            continue
        try:
            total_bytes += len(item["content"].encode("utf-8"))
        except UnicodeEncodeError:
            return [], [], f"Error: the content of '{path}' is not valid UTF-8 text, so it cannot be committed."
        writes.append((path, item["content"], executable))
    # The byte cap of one signed commit is weighed by the caller once every
    # file is read: a change set over it is split into signed commits (#764).
    deletes: list[str] = []
    for raw in raw_deletes:
        path, err = _commit_path(raw, "delete_paths")
        if err:
            return [], [], err
        if path in seen:
            return [], [], f"Error: '{path}' is listed more than once (a path is either written or deleted)."
        seen.add(path)
        deletes.append(path)
    # One tree cannot hold a file and a directory under the same name, and a
    # path absent from the branch escapes the tree lookups: refuse it here.
    for path in [path for path, _, _ in writes] + deletes:
        segments = path.split("/")
        for depth in range(1, len(segments)):
            ancestor = "/".join(segments[:depth])
            if ancestor in seen:
                return [], [], (
                    f"Error: '{ancestor}' and '{path}' are both in this commit; a path cannot be a file and a "
                    "directory at once. Commit them separately."
                )
    # Two names that differ only in case collide on a case-insensitive checkout
    # (Windows, macOS). A deleted path is not in the commit's tree, so writing
    # 'README.md' while deleting 'readme.md' (a rename) is fine.
    spellings: dict[str, str] = {}
    for path, _, _ in writes:
        segments = path.split("/")
        for depth in range(1, len(segments) + 1):
            prefix = "/".join(segments[:depth])
            first = spellings.setdefault(prefix.casefold(), prefix)
            if first != prefix:
                return [], [], (
                    f"Error: '{first}' and '{prefix}' differ only in case, so they collide on a case-insensitive "
                    "checkout (Windows, macOS). Use one spelling."
                )
    return writes, deletes, None


def _unwritable(entry: dict[str, Any], path: str, repo: str) -> str | None:
    """Why *path* cannot be written over what it is now, or ``None`` for a file."""
    kind = entry.get("type")
    if kind == "tree":
        return f"Error: '{path}' is a directory in {repo}; a file cannot replace it."
    if kind == "commit":
        return f"Error: '{path}' is a submodule in {repo}; it is not overwritten with a file."
    if _coerce_str(entry.get("mode")) == _MODE_SYMLINK:
        return f"Error: '{path}' is a symbolic link in {repo}; it is not overwritten with a file."
    return None


def _undeletable(entry: dict[str, Any], path: str, repo: str) -> str | None:
    """Why *path* cannot be deleted as a file, or ``None`` when it can."""
    kind = entry.get("type")
    if kind == "tree":
        return f"Error: '{path}' is a directory in {repo}; list the files inside it to delete them."
    if kind == "commit":
        return f"Error: '{path}' is a submodule in {repo}; it is not removed by this tool."
    return None


def _moved_tip_refusal(branch: str, repo: str, current: str) -> str:
    """The answer when the branch no longer points at the tip the change was built on."""
    return (
        f"Error: branch '{branch}' of {repo} changed while the commit was being built (it now points at "
        f"{current[:8] or 'an unreadable commit'}), so it was not moved and is unchanged. Call the tool again to "
        "commit on top of the new tip."
    )


#: The SHA in the first line of a ``create_github_commit`` result that landed.
_COMMITTED_RE = re.compile(r"^Committed \d+ file change\(s\) to branch .* in one commit \(([0-9a-f]{40})\)")
#: The branch and SHA in the first line of a ``create_github_commit`` result that landed.
_COMMITTED_ON_RE = re.compile(
    r"^Committed \d+ file change\(s\) to branch '(.*)' of \S+ in one commit \(([0-9a-f]{40})\)"
)
#: What ``create_github_commit`` answers for a repository with no commit yet.
_EMPTY_REPOSITORY_MARK = "is empty (it has no commit yet)"

#: One lock per (repository, branch) for the writes of this worker (#12051).
#: Three writes on one branch in one round each read the same tip: one landed,
#: the others were refused on a stale sha. Under the lock each write reads the
#: tip the previous one left. A lock lives while a write holds or awaits it.
_BRANCH_LOCKS: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = weakref.WeakValueDictionary()


@contextlib.asynccontextmanager
async def branch_write_lock(repo: str, branch: Any) -> AsyncIterator[None]:
    """Serialize the writes of this worker to one repository branch, in call order.

    The key is the repository (case-folded) and the branch as given: an
    omitted branch shares the key of the other calls that omit it.
    """
    key = (_coerce_str(repo).strip().casefold(), _coerce_str(branch).strip())
    lock = _BRANCH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _BRANCH_LOCKS[key] = lock
    async with lock:
        yield


#: How the signing line of a commit result starts (``_graphql_signing_note``, ``_git_data_signing_note``).
_SIGNING_PREFIXES = ("GitHub built", "Signed", "Not signed", "Signature not reported")


def checkout_batches(sizes: dict[str, int], *, first_room: int | None = None) -> list[list[str]]:
    """The paths of ``{path: bytes}`` in consecutive commits, each under the caps of one signed commit.

    Smallest files first, filled in order (next fit): the code and the
    tests of a change land in the first commit, the large files (i18n
    catalogs) after them. Each commit stays under ``_MAX_COMMIT_CONTENT_BYTES``
    and ``_MAX_COMMIT_CHANGES``; *first_room* lowers the change count of the
    first commit (the deletions that ride with it take the rest). A file over
    the byte cap on its own is refused by the caller before any write; here it
    would get a commit of its own.
    """
    batches: list[list[str]] = []
    used = 0
    for path in sorted(sizes, key=lambda item: (sizes[item], item)):
        size = sizes[path]
        room = first_room if first_room is not None and len(batches) == 1 else _MAX_COMMIT_CHANGES
        if batches and used + size <= _MAX_COMMIT_CONTENT_BYTES and len(batches[-1]) < room:
            batches[-1].append(path)
            used += size
        else:
            batches.append([path])
            used = size
    return batches


def checkout_commit_plan(sizes: dict[str, int], deletes: list[str]) -> list[tuple[list[str], list[str]]]:
    """``(written paths, deleted paths)`` of each consecutive commit of a split ``from_checkout`` change set.

    Deletions weigh nothing: the last ``_MAX_COMMIT_CHANGES``-sized share of
    them rides with the first batch of writes (which gets that much less
    room), and any full share before it is a commit of deletions alone. Every
    commit carries at most ``_MAX_COMMIT_CHANGES`` changes.
    """
    cap = _MAX_COMMIT_CHANGES
    full = (len(deletes) // cap) * cap
    plan: list[tuple[list[str], list[str]]] = [([], deletes[start : start + cap]) for start in range(0, full, cap)]
    tail = deletes[full:]
    batches = checkout_batches(sizes, first_room=cap - len(tail))
    if batches:
        plan.append((batches[0], tail))
        plan.extend((batch, []) for batch in batches[1:])
    elif tail:
        plan.append(([], tail))
    return plan


def _batch_line(paths: list[str], deletes: list[str]) -> str:
    parts = [_listed_paths("written", paths)] if paths else []
    if deletes:
        parts.append(_listed_paths("deleted", deletes))
    return "; ".join(parts)


def _listed_paths(label: str, paths: list[str]) -> str:
    shown = ", ".join(paths[:_MAX_LISTED_COMMIT_PATHS])
    more = len(paths) - _MAX_LISTED_COMMIT_PATHS
    return f"{label}: {shown}" + (f" (and {more} more)" if more > 0 else "")


#: GitHub builds - and signs - a commit made through this mutation. It moves
#: the branch only while it still points at ``expectedHeadOid``, atomically.
#: It takes no file mode: an addition replaces the file at its path. The
#: commit's signature comes back with it, so the result says what GitHub says.
_CREATE_COMMIT_MUTATION = (
    "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) "
    "{ commit { oid signature { isValid state } } } }"
)

#: A word GitHub uses for its signature verdict ('valid', 'unsigned', 'unknown_key', ...).
_VERIFICATION_REASON_RE = re.compile(r"[a-z_]{1,40}")
_REFUSED_UNLESS_VERIFIED = "a branch that requires signed commits refuses it"


class _SignatureVerdict(NamedTuple):
    """GitHub's verdict on the signature of a commit."""

    signed: bool
    verified: bool
    #: GitHub's word for it ('valid', 'unsigned', 'unknown_key', ...), or ''.
    reason: str


def _verdict_word(value: Any) -> str:
    word = _coerce_str(value).strip().lower()
    return word if _VERIFICATION_REASON_RE.fullmatch(word) else ""


def _rest_signature_verdict(commit: dict[str, Any]) -> _SignatureVerdict | None:
    """The verdict in a REST commit's ``verification``, ``None`` when it has none.

    ``verified`` is false for an unsigned commit and for a signature GitHub
    cannot verify alike: the signature itself tells the two apart.
    """
    verification = commit.get("verification")
    if not isinstance(verification, dict) or not isinstance(verification.get("verified"), bool):
        return None
    reason = _verdict_word(verification.get("reason"))
    verified = verification["verified"]
    signed = verified or bool(_coerce_str(verification.get("signature")).strip()) or reason not in ("", "unsigned")
    return _SignatureVerdict(signed, verified, reason)


def _graphql_signature_verdict(commit: dict[str, Any]) -> _SignatureVerdict | None:
    """The verdict in a GraphQL commit's ``signature`` (null when unsigned), ``None`` when it has none."""
    if "signature" not in commit:
        return None
    signature = commit["signature"]
    if signature is None:
        return _SignatureVerdict(False, False, "unsigned")
    if not isinstance(signature, dict) or not isinstance(signature.get("isValid"), bool):
        return None
    return _SignatureVerdict(True, signature["isValid"], _verdict_word(signature.get("state")))


def _graphql_signing_note(verdict: _SignatureVerdict | None) -> str:
    """Whether the commit ``createCommitOnBranch`` made is signed, as GitHub reports it."""
    if verdict is None:
        return (
            "GitHub built this commit itself, which it signs; its answer did not say whether the signature is "
            "verified."
        )
    if verdict.verified:
        return "GitHub built and signed this commit itself, and reports it as verified."
    if verdict.signed:
        said = f" ({verdict.reason})" if verdict.reason else ""
        return (
            f"GitHub built and signed this commit itself, but reports the signature as not verified{said}; "
            f"{_REFUSED_UNLESS_VERIFIED}."
        )
    return f"GitHub built this commit itself but reports it as unsigned; {_REFUSED_UNLESS_VERIFIED}."


def _git_data_path_note(reason: str) -> str:
    """Why a change goes through the Git Data API, said before its commit exists."""
    return f"The Git Data API was used because {reason}."


def _git_data_signing_note(reason: str, verdict: _SignatureVerdict | None) -> str:
    """Whether a commit made through the Git Data API is signed, as GitHub reports it.

    GitHub signs one only for a GitHub App acting as itself; the user
    credentials this integration holds get it unsigned.
    """
    path = f"{reason}, so the commit was built through the Git Data API"
    if verdict is None:
        return (
            f"Signature not reported: {path}, and GitHub did not say whether it is signed; "
            f"{_REFUSED_UNLESS_VERIFIED} unless it is verified."
        )
    if verdict.verified:
        return f"Signed: {path}, and GitHub reports it as verified."
    if verdict.signed:
        said = f" ({verdict.reason})" if verdict.reason else ""
        return (
            f"Signed but not verified: {path}, and GitHub reports the signature as not verified{said}; "
            f"{_REFUSED_UNLESS_VERIFIED}."
        )
    return f"Not signed: {path}, and GitHub reports it as unsigned; {_REFUSED_UNLESS_VERIFIED}."


def _requires_signed_commits(resp: httpx.Response | None) -> bool:
    """True when GitHub refused a branch move because the branch requires signed commits."""
    if resp is None:
        return False
    text = resp.text.lower()
    return "signature" in text or "signed commit" in text


def _retry_note(landed: str, deletes: list[str]) -> str:
    """Why calling the tool again after an unclear outcome cannot apply the change twice."""
    if deletes:
        after = f"that call commits nothing and answers that '{deletes[0]}' does not exist, since it is already deleted"
    else:
        after = "that call finds the change already made and commits nothing"
    return f"Calling the tool again with the same change cannot apply it twice: if {landed}, {after}."


def _mode_reason(path: str, current: str | None, mode: str) -> str | None:
    """Why writing *path* with *mode* needs a file mode, or ``None`` when it does not.

    *current* is the mode on the branch, ``None`` for a new file. A file whose
    mode does not change needs none: a ``createCommitOnBranch`` addition that
    overwrites an existing path keeps that path's mode (an executable script
    stays 100755, verified on GitHub, #10444), but it cannot set or clear the
    executable bit, nor create a file executable.
    """
    if mode == current or (mode == _MODE_FILE and current is None):
        return None
    if current is None:
        return f"'{path}' is created executable"
    if current == _MODE_EXECUTABLE:
        return f"'{path}' loses its executable bit"
    if current == _MODE_FILE:
        return f"'{path}' becomes executable"
    return f"'{path}' has file mode {current} and is written as {mode}"


def _graphql_error_text(errors: list[Any]) -> tuple[str, set[str]]:
    """The messages of a GraphQL ``errors`` array, and its error types / codes."""
    messages: list[str] = []
    kinds: set[str] = set()
    for error in errors:
        if not isinstance(error, dict):
            continue
        if error.get("message"):
            messages.append(str(error["message"]))
        extensions = error.get("extensions")
        for kind in (error.get("type"), extensions.get("code") if isinstance(extensions, dict) else None):
            if isinstance(kind, str):
                kinds.add(kind.upper())
    return "; ".join(messages) or "unknown error", kinds


def _signing_unavailable(message: str, kinds: set[str]) -> bool:
    """True when the server does not offer ``createCommitOnBranch`` at all.

    Only an error naming the mutation says so: a field of its selection that a
    server lacked must not turn the change into an unsigned commit.
    """
    low = message.lower()
    return "createcommitonbranch" in low and ("UNDEFINEDFIELD" in kinds or "doesn't exist" in low)


def _graphql_commit_refusal(message: str, kinds: set[str], branch: str, repo: str, tip: str) -> str:
    """The tool result for a ``createCommitOnBranch`` that GitHub refused.

    A refused mutation changes nothing, so every answer says the branch is
    unchanged; the wording matches the Git Data API path's for the same cause.
    """
    low = message.lower()
    if "expected branch to point to" in low or "expectedheadoid" in low:
        return (
            f"Error: branch '{branch}' of {repo} changed while the commit was being built (it no longer points at "
            f"{tip[:8]}), so it was not moved and is unchanged. Call the tool again to commit on top of the new tip."
        )
    if "RATE_LIMITED" in kinds or "rate limit" in low:
        return (
            f"Error: GitHub rate limit exceeded while trying to commit to branch '{branch}' of {repo}; the branch is "
            "unchanged. Wait a moment and try again."
        )
    if any(word in low for word in ("protected branch", "rule violation", "pull request", "status check")):
        return (
            f"Error: GitHub refused the commit on branch '{branch}' of {repo}: {message}. The branch is unchanged: "
            "it is protected (commit to a branch made with create_github_branch, then open a pull request)."
        )
    if "FORBIDDEN" in kinds or any(word in low for word in ("not accessible", "permission", "push access")):
        return (
            f"Error: GitHub access denied while trying to commit to branch '{branch}' of {repo}: {message}. The "
            "branch is unchanged; the integration needs write access to the repository's contents."
        )
    if "NOT_FOUND" in kinds or "could not resolve" in low:
        return f"Error: GitHub could not find branch '{branch}' of {repo}: {message}. The branch is unchanged."
    return f"Error: GitHub refused the commit on branch '{branch}' of {repo}: {message}. The branch is unchanged."


#: Said after a refused signed commit: the per-file tool is not a way around it.
_NO_UNSIGNED_FALLBACK = (
    " Do not write the change with create_or_update_github_file instead: GitHub does not sign a Contents API "
    "commit made with this integration's credential, and a branch that requires signed commits refuses it."
)

#: GraphQL error types / codes GitHub answers for a failure on its side.
_TRANSIENT_GRAPHQL_KINDS = frozenset({"INTERNAL", "INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE", "TIMEOUT"})
#: Messages GitHub answers for a failure on its side, lowercased.
_TRANSIENT_GRAPHQL_MESSAGES = ("something went wrong while executing your query", "timedout", "timed out")


async def _read_branch_tip(
    client: httpx.AsyncClient, repo_url: str, headers: dict[str, str], branch: str, repo: str
) -> str:
    """The commit ``branch`` points at now, '' when it cannot be read."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/git/ref/heads/{_enc(branch)}",
        _PROVIDER,
        f"re-read branch {branch} of {repo}",
        headers=headers,
    )
    if err or resp is None:
        return ""
    target = _json_object(resp).get("object")
    sha = _coerce_str(target.get("sha")) if isinstance(target, dict) else ""
    return sha if _GIT_OBJECT_ID_RE.fullmatch(sha) else ""


def _head_moved(message: str) -> bool:
    low = message.lower()
    return "expected branch to point to" in low or "expectedheadoid" in low


#: HTTP statuses after which a mutation may have landed: a 5xx, and the 499 a
#: proxy answers when it closed the request before GitHub's answer came back.
_ANSWER_UNKNOWN_CLIENT_STATUSES = frozenset({499})


def _answer_unknown_status(status: int) -> bool:
    """True when a failed commit request says nothing of whether GitHub applied it."""
    return status >= 500 or status in _ANSWER_UNKNOWN_CLIENT_STATUSES


def _transient_commit_failure(resp: httpx.Response | None, err: str | None) -> bool:
    """True when a ``createCommitOnBranch`` attempt failed on GitHub's side and may be tried once more.

    A 5xx, no answer at all, or a GraphQL error of a server-side kind with no
    commit in the answer. A refusal (protected branch, moved tip, access,
    rate limit) is not transient: trying again changes nothing.
    """
    if err:
        return resp is None or _answer_unknown_status(resp.status_code)
    answer = _json_object(resp)
    data = answer.get("data")
    created = data.get("createCommitOnBranch") if isinstance(data, dict) else None
    if isinstance(created, dict) and isinstance(created.get("commit"), dict):
        return False
    errors = answer.get("errors")
    if not isinstance(errors, list) or not errors:
        return False
    message, kinds = _graphql_error_text(errors)
    low = message.lower()
    return bool(kinds & _TRANSIENT_GRAPHQL_KINDS) or any(word in low for word in _TRANSIENT_GRAPHQL_MESSAGES)


def _contents_signing_note(commit: dict[str, Any], branch: str) -> str:
    """What a Contents API answer says of the commit's signature, '' when it is verified or not reported.

    GitHub signs a Contents API commit only for a GitHub App acting as
    itself: with a user credential it lands unsigned, which a branch that
    requires signed commits refuses.
    """
    verdict = _rest_signature_verdict(commit)
    if verdict is None or verdict.verified:
        return ""
    state = "signed but not verified" if verdict.signed else "not signed"
    said = f" ({verdict.reason})" if verdict.reason else ""
    return (
        f"\nWarning: GitHub reports this commit as {state}{said}. If branch '{branch}' requires signed commits, "
        "its checks refuse it and only a new branch from the last signed commit clears it. create_github_commit "
        "writes the same change as a commit GitHub builds and signs."
    )


async def _changes_from_checkout(
    directory: str, paths: list[str] | None, repo: str
) -> tuple[list[dict[str, Any]], list[str], str, Any] | str:
    """The ``files`` and ``delete_paths`` of a ``from_checkout`` commit, the note for the result and the record.

    The record is handed back so the commit can move it to the new commit
    (``WorkspaceSink.advance_checkout``).

    Read through the run's sandbox workspace (``current_workspace()``): the
    checkout record names what was checked out, the manifest what the
    sandbox holds now. A checkout of another repository is refused: the
    change set would not mean anything on *repo*.
    """
    workspace = current_workspace()
    if workspace is None:
        return "Error: from_checkout needs the sandbox workspace of this run, and this run has none."
    try:
        record, changes = await workspace.checkout_changes(directory, list(paths or []) or None)
    except ValueError as exc:
        return f"Error: from_checkout refused: {exc}"
    if record.repository and record.repository.casefold() != repo.strip().casefold():
        return (
            f"Error: {directory} is a checkout of {record.repository}, not of {repo}: the change set belongs to "
            "that repository."
        )
    origin = f"{record.repository}@{record.ref or 'default branch'}" + (
        f" (commit {record.commit[:12]})" if record.commit else ""
    )
    if changes.empty:
        note = f"Nothing to commit: {directory} is unchanged since its checkout of {origin}."
        return [], [], note + changes.untracked_note(record.path) + changes.ignored_note(), record
    if changes.untracked and not paths:
        # A commit of the rest would leave these behind without a word anyone reads (#237).
        subs = ", ".join(f'"{s}"' for s in record.sub_paths)
        return (
            f"Error: from_checkout refused, nothing was committed: {changes.describe()}, and "
            f"{len(changes.untracked)} file(s) changed since the checkout lie outside its record and sub_paths "
            f"({', '.join(changes.untracked[:12])}{', ...' if len(changes.untracked) > 12 else ''}), so the commit "
            f"would be partial. To commit them, pass paths=[{subs}, and those files or their directories]; to "
            f"leave them out, pass paths=[{subs}]; or grow the checkout with "
            f'materialize_repository(path="{record.path}", add_paths=[...]).'
        )
    if changes.count > _MAX_CHECKOUT_SPLIT_CHANGES:
        return (
            f"Error: {changes.count} files changed under {directory}, over the {_MAX_CHECKOUT_SPLIT_CHANGES} one call "
            f"splits into signed commits ({changes.describe()}); narrow it with 'paths' or commit in several calls."
        )
    files: list[dict[str, Any]] = []
    total = 0
    for repo_path in [*changes.modified, *changes.added]:
        try:
            _rel, _name, data = await workspace.read_file(record.workspace_path(repo_path), tool="create_github_commit")
        except ValueError as exc:
            return f"Error: from_checkout could not read {repo_path}: {exc}"
        if len(data) > _MAX_COMMIT_CONTENT_BYTES:
            # No split brings one file under the cap of one signed commit.
            return (
                f"Error: {repo_path} is {len(data)} bytes, over the {_MAX_COMMIT_CONTENT_BYTES} one signed commit may "
                "carry, so no split can commit it; nothing was committed."
            )
        total += len(data)
        if total > _MAX_CHECKOUT_SPLIT_BYTES:
            return (
                f"Error: the changed files under {directory} add up to over {_MAX_CHECKOUT_SPLIT_BYTES} bytes, the "
                "most one call splits into signed commits; narrow it with 'paths' or commit in several calls."
            )
        files.append({"path": repo_path, "content": _WorkspaceBytes(data)})
    note = f"From the checkout of {origin} at {directory}: {changes.describe()}." + changes.untracked_note(record.path) + changes.ignored_note()
    # A pre-push guard (tsc, knip) the change touches and the sandbox did not see pass on this tree: said first.
    guard = getattr(workspace, "prepush_warning", None)
    warned = await guard(record, changes) if guard is not None else ""
    if warned:
        note = f"{warned}\n{note}"
    return files, list(changes.removed), note, record


async def _adopt_landed_commit(
    headers: dict[str, str], repo: str, owner: str, name: str, branch: str, record: Any
) -> str:
    """The checkout *record* moved to the branch tip when the tip is the commit its pending note describes (#12377).

    A ``from_checkout`` call notes in the record the commit it is about to
    make; cut by a rollout after the commit landed, it never moved the record.
    The next call reads the tip once: when the tip's parent is the noted parent
    and the tip changed exactly the noted files with the noted blobs, the tip
    is that commit, and the record moves to it. The sentence for the result,
    ``""`` when nothing moved (the drift reconcile then runs as before).
    """
    from app.services.sandbox_workspace.checkout import pending_matches

    workspace = current_workspace()
    adopt = getattr(workspace, "adopt_landed_checkout", None)
    pending = getattr(record, "pending", None)
    if adopt is None or not isinstance(pending, dict) or not pending.get("parent"):
        return ""
    target = branch or _coerce_str(pending.get("branch"))
    if not target or (branch and _coerce_str(pending.get("branch")) not in ("", branch)):
        return ""
    async with egress_client(timeout=30) as client:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/commits/{_enc(target)}",
            _PROVIDER,
            f"read the tip of {target} in {repo}",
            headers=headers,
            expected_statuses={404, 422},
        )
    data = _json_object(resp) if not err else {}
    tip = _coerce_str(data.get("sha"))
    parents = data.get("parents")
    listed = data.get("files")
    if not _GIT_OBJECT_ID_RE.fullmatch(tip) or not isinstance(parents, list) or len(parents) != 1:
        return ""
    if not isinstance(listed, list) or len(listed) >= 300:
        # GitHub lists 300 files of a commit at most: a longer list cannot be compared whole.
        return ""
    parent = _coerce_str(parents[0].get("sha")) if isinstance(parents[0], dict) else ""
    changed: dict[str, str | None] = {}
    for item in listed:
        if not isinstance(item, dict):
            return ""
        path = _coerce_str(item.get("filename"))
        status = _coerce_str(item.get("status"))
        if status == "renamed":
            previous = _coerce_str(item.get("previous_filename"))
            if previous:
                changed[previous] = None
        changed[path] = None if status == "removed" else _coerce_str(item.get("sha"))
    if not pending_matches(record, parent=parent, changed=changed):
        return ""
    return str(await adopt(record, commit=tip) or "")


async def _note_pending_commit(
    record: Any, branch: str, parent: str, files: list[dict[str, Any]] | None, delete_paths: list[str] | None
) -> None:
    """Note in the checkout *record* the commit about to be made on *branch* over *parent* (#12377)."""
    workspace = current_workspace()
    note = getattr(workspace, "note_checkout_pending", None)
    if record is None or note is None or not _GIT_OBJECT_ID_RE.fullmatch(parent or ""):
        return
    written = {
        str(item["path"]): item["content"].data
        for item in files or []
        if isinstance(item, dict) and isinstance(item.get("content"), _WorkspaceBytes)
    }
    await note(record, branch=branch, parent=parent, written=written, removed=list(delete_paths or []))


#: A commit id as an archive or a person writes it: 7 to 40 hex characters (a full id is 40, or 64 for sha256).
_SHORT_OBJECT_ID_RE = re.compile(r"[0-9a-f]{7,40}(?:[0-9a-f]{24})?")


async def _full_commit_id(client: Any, headers: dict[str, str], repo: str, repo_url: str, short: str) -> str:
    """The full id of commit *short* in *repo*, ``""`` when GitHub does not name exactly one that starts with it."""
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{repo_url}/commits/{_enc(short)}",
        _PROVIDER,
        f"read commit {short} of {repo}",
        headers=headers,
        expected_statuses={404, 422},
    )
    sha = _coerce_str(_json_object(resp).get("sha")).lower() if not err else ""
    return sha if _GIT_OBJECT_ID_RE.fullmatch(sha) and sha.startswith(short) else ""


async def _reconcile_checkout_drift(
    headers: dict[str, str],
    repo: str,
    owner: str,
    name: str,
    branch: str,
    directory: str,
    record: Any,
    files: list[dict[str, Any]],
    delete_paths: list[str],
) -> tuple[list[dict[str, Any]], list[str], str, dict[str, bytes], str, bool] | str:
    """The ``from_checkout`` change set made safe for the branch tip (``checkout_drift.reconcile``).

    ``(files, delete_paths, note, workspace rewrites, tip merged against,
    keep the record's commit)``, or the refusal. The last is true when the
    branch changed files of the checkout this commit does not carry: the
    record then stays at its commit, so the next commit merges them too
    (#10061). The tip is read here; when it is the record's commit and no
    commit made from the checkout moved the record past the commit the tree
    was read at, nothing else is requested and the change set is returned as
    it is. A moved record (``base_commit`` set and not ``commit``) names a
    commit whose files the workspace never held for the paths those commits
    did not write, so they are merged against the baseline even then (#1040).
    """
    from .checkout_drift import reconcile

    recorded = _coerce_str(getattr(record, "commit", "")).lower()
    if not _SHORT_OBJECT_ID_RE.fullmatch(recorded):
        return files, delete_paths, "", {}, "", False
    baseline = _coerce_str(getattr(record, "base_commit", "")).lower()
    if not _SHORT_OBJECT_ID_RE.fullmatch(baseline):
        baseline = ""
    repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
    async with egress_client(timeout=60) as client:
        # A checkout record written from an archive names the commit by the archive's short id
        # (``<owner>-<repo>-<sha7>``): read the full id, or the drift merge below never runs and the
        # workspace files are written whole over a tip that moved, reverting it (#2152).
        for label in ("recorded", "baseline"):
            short = recorded if label == "recorded" else baseline
            if not short or _GIT_OBJECT_ID_RE.fullmatch(short):
                continue
            full = await _full_commit_id(client, headers, repo, repo_url, short)
            if not full:
                return (
                    f"Error: the checkout record of {directory} names commit {short}, which could not be read in "
                    f"{repo}, so its files cannot be merged with branch '{branch}' and nothing was committed: "
                    "writing them whole could revert the branch's changes. "
                    f'Run sandbox_checkout_rebase(checkout="{directory}", ref="{branch}"), then commit again.'
                )
            if label == "recorded":
                recorded = full
            else:
                baseline = full
        if baseline == recorded:
            baseline = ""
        resolved, err = await _resolve_branch(client, headers, repo, owner, name, branch)
        if err or not resolved or _branch_name_error(resolved):
            # The commit itself reads the branch again and says what is wrong.
            return files, delete_paths, "", {}, "", False
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{repo_url}/git/ref/heads/{_enc(resolved)}",
            _PROVIDER,
            f"read branch {resolved} of {repo}",
            headers=headers,
            expected_statuses={404, 409},
        )
        ref_object = _json_object(resp).get("object") if not err else None
        tip = _coerce_str(ref_object.get("sha")) if isinstance(ref_object, dict) else ""
        if not _GIT_OBJECT_ID_RE.fullmatch(tip) or (tip == recorded and not baseline):
            return files, delete_paths, "", {}, "", False
        writes = {str(item["path"]): item["content"].data for item in files}
        done = await reconcile(
            client,
            headers,
            repo,
            repo_url,
            branch=resolved,
            tip=tip,
            recorded=recorded,
            checkout=directory,
            writes=writes,
            deletes=list(delete_paths),
            tracked=list(getattr(record, "files", None) or ()),
            recorded_files={
                str(path): _coerce_str(getattr(meta, "sha256", ""))
                for path, meta in (getattr(record, "files", None) or {}).items()
            },
            baseline=baseline,
            committed=list(getattr(record, "branch", None) or ()),
        )
    if isinstance(done, str):
        return done
    merged_files = [{"path": path, "content": _WorkspaceBytes(data)} for path, data in done.writes.items()]
    return merged_files, done.deletes, done.note, done.merged, tip, bool(done.stale)


async def _reconcile_workspace_writes(
    headers: dict[str, str],
    repo: str,
    owner: str,
    name: str,
    branch: str,
    written: dict[str, tuple[str, bytes]],
) -> tuple[dict[str, bytes], str, str] | str:
    """The ``files[].workspace_path`` writes of checkout files made safe for the branch tip (#11852).

    ``(repository path -> bytes to commit, note, tip merged against)``, or the
    refusal. A file read from a ``materialize_repository`` checkout is the
    checkout's text plus the edits: when the branch moved past the record's
    commit and changed that file since, writing it whole would revert the
    branch, so it goes through the same three-way merge as ``from_checkout``
    (:func:`_reconcile_checkout_drift`). A file outside any checkout, of
    another repository's checkout or committed at another path is left as it is.
    """
    workspace = current_workspace()
    lookup = getattr(workspace, "checkout_records_for", None)
    if not written or lookup is None:
        return {}, "", ""
    try:
        records = await lookup(list(written))
    except Exception as exc:  # no record read: nothing to merge against, the commit proceeds as before
        logger.warning("checkout lookup for a files commit failed: %s", format_exception_message(exc))
        return {}, "", ""
    grouped: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
    for workspace_path, (repo_path, data) in written.items():
        record = records.get(workspace_path)
        if record is None or not _SHORT_OBJECT_ID_RE.fullmatch(_coerce_str(getattr(record, "commit", "")).lower()):
            continue
        recorded_repo = _coerce_str(getattr(record, "repository", ""))
        if recorded_repo and recorded_repo.casefold() != repo.strip().casefold():
            continue
        if record.repo_path(workspace_path) != repo_path:
            continue
        _record, items = grouped.setdefault(record.path, (record, []))
        items.append({"path": repo_path, "content": _WorkspaceBytes(data)})
    replaced: dict[str, bytes] = {}
    notes: list[str] = []
    merged_on = ""
    for directory, (record, items) in grouped.items():
        done = await _reconcile_checkout_drift(headers, repo, owner, name, branch, directory, record, items, [])
        if isinstance(done, str):
            return done
        merged_files, _deletes, note, merged, tip, _stale = done
        if tip:
            if merged_on and tip != merged_on:
                return _moved_tip_refusal(branch, repo, tip)
            merged_on = tip
        for item in merged_files:
            replaced[str(item["path"])] = item["content"].data
        if merged:
            # A files[] commit never rewrites the workspace: say so instead of the from_checkout sentence.
            note = note.replace(
                "The workspace files now hold the merged text.",
                f"The workspace files still hold their own text: run "
                f'sandbox_checkout_rebase(checkout="{directory}", ref="{branch}") before editing them again.',
            )
        if note:
            notes.append(note)
    return replaced, " ".join(notes), merged_on


async def _advance_checkout(
    record: Any,
    commit: str,
    branch: str,
    files: list[dict[str, Any]] | None,
    delete_paths: list[str] | None,
    rewrites: dict[str, bytes] | None = None,
) -> str:
    """Move the ``from_checkout`` record to *commit* on *branch*; the sentence for the result.

    *rewrites* are the workspace files a merge with the branch changed: the
    sink writes them with the record, so the workspace holds what was committed.
    """
    workspace = current_workspace()
    advance = getattr(workspace, "advance_checkout", None)
    if record is None or advance is None:
        return ""
    written = {
        str(item["path"]): item["content"].data
        for item in files or []
        if isinstance(item, dict) and isinstance(item.get("content"), _WorkspaceBytes)
    }
    if rewrites:
        return str(
            await advance(
                record, commit=commit, ref=branch, written=written, removed=list(delete_paths or []), rewrites=rewrites
            )
        )
    return str(await advance(record, commit=commit, ref=branch, written=written, removed=list(delete_paths or [])))


def _split_files_change_set(
    staged: list[tuple[str, Any, bool | None]], total_bytes: int
) -> list[dict[str, Any]] | str:
    """The ``files[]`` writes of a change set over the byte cap, ready for the split path; or the refusal.

    An ``edits`` entry is resolved against the branch tip of one commit, and a
    single file over the cap fits no commit: both keep the refusal (#764).
    """
    if any(isinstance(content, _Edits) for _path, content, _x in staged):
        return (
            f"Error: the files add up to {total_bytes} bytes, over the {_MAX_COMMIT_CONTENT_BYTES} one commit "
            "may carry, and an 'edits' entry cannot be split; commit the edits apart."
        )
    batch: list[dict[str, Any]] = []
    for path, content, executable in staged:
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        if len(data) > _MAX_COMMIT_CONTENT_BYTES:
            return (
                f"Error: {path} is {len(data)} bytes, over the {_MAX_COMMIT_CONTENT_BYTES} one signed commit may "
                "carry, so no split can commit it; nothing was committed."
            )
        item: dict[str, Any] = {"path": path, "content": _WorkspaceBytes(data)}
        if executable is not None:
            item["executable"] = executable
        batch.append(item)
    return batch


async def _advance_checkout_files(
    repo: str,
    branch: str,
    parent: str,
    commit: str,
    written: dict[str, tuple[str, bytes]],
    committed: list[str],
) -> str:
    """Move the checkout record a ``files[]`` commit of its own workspace files was made from; the sentence, or "".

    Without it, the record stays at its commit and the next ``from_checkout``
    of a file edited again refuses as an overlap with the branch.
    """
    workspace = current_workspace()
    advance = getattr(workspace, "advance_checkout_files", None)
    if not written or advance is None or not _GIT_OBJECT_ID_RE.fullmatch(parent or ""):
        return ""
    return str(
        await advance(
            repository=repo, branch=branch, parent=parent, commit=commit, written=written, committed=committed
        )
    )


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
    persist_file_fn: PersistFileFn | None = None,
    record_push: Any = None,
) -> list[ToolDefinition]:
    """Build the GitHub contents tools and register their callables.

    *record_push* is the platform's ``(repo, number, head_branch, head_sha, url,
    draft) -> str`` that names an open pull request on the work item the run was
    dispatched for; ``None`` outside such a run. A commit pushed to a branch with
    an open pull request then names it, so the run's end parks the item on that
    pull request instead of releasing it with nothing to wait for (#9819).
    """

    async def name_pushed_pull_request(repo: str, owner: str, name: str, branch: str, sha: str) -> str:
        """After a push to *branch*: start the full CI its open pull request is gated on, and name
        that pull request on the run's work item; ``""`` when there is none. Never raises.

        The full run starts here, in parallel with the pull_request checks the push
        triggers, instead of at the first merge call (#12067).
        """
        if not branch or not sha:
            return ""
        try:
            async with egress_client(timeout=30) as client:
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/pulls",
                    _PROVIDER,
                    f"find the open pull request of {branch} in {repo}",
                    headers=headers,
                    params={"head": f"{owner}:{branch}", "state": "open", "per_page": "1"},
                )
                pulls = resp.json() if not err and resp is not None else None
                if not isinstance(pulls, list) or not pulls or not isinstance(pulls[0], dict):
                    return ""
                pr = pulls[0]
                started = await full_ci_gate.start_on_push(client, owner, name, pr, headers, head_sha=sha)
            number = _coerce_int(pr.get("number"))
            if not number:
                return started
            if record_push is None:
                return started
            draft = pr.get("draft") if isinstance(pr.get("draft"), bool) else None
            named = _coerce_str(await record_push(f"{owner}/{name}", number, branch, sha, pr.get("html_url"), draft))
            return "\n".join(part for part in (named, started) if part)
        except Exception as exc:
            logger.warning(
                "Could not name the pull request of %s on %s: %s", branch, repo, format_exception_message(exc)
            )
            return ""

    async def get_github_file(
        repo: str, path: str, branch: str = "", offset: int = 0, max_chars: int = _DEFAULT_FILE_CHARS
    ) -> str | ToolResult:
        """Read a file's content from a repository, a window of it at a time."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if workspace_ingest_requested():
            # Bound for the sandbox workspace: its raw bytes, whatever
            # its size or type, never the rendering below.
            return await download_github_file(repo, path, branch)
        offset, max_chars = _clamp_window(offset, max_chars)
        # The tool-test endpoint bypasses jsonschema coercion, so `path` and
        # `branch` can arrive as non-strings (null, number, object) - coerce
        # before strip. A blank/non-string path would otherwise hit the repo
        # root at `/contents/` with confusing "read file None" messages, so
        # fail fast with a friendly required-field error instead.
        path = _coerce_str(path).strip()
        if not path:
            return "Error: 'path' is required (file path relative to the repository root)."
        contents_url = f"{_BASE_URL}/repos/{owner}/{name}/contents/{_enc(path)}"

        # Negative-lookup cache fast-path, restricted to allow-listed
        # convention files (see _NEGATIVE_CACHE_PATHS). Keyed on the branch as
        # *requested* (see _branch_cache_token) so a cached miss
        # short-circuits before default-branch resolution — zero HTTP calls,
        # including the repo-metadata lookup. Fails open: a Redis outage
        # simply falls through to the normal HTTP path.
        cacheable = _is_negative_cacheable(path)
        requested_branch = _coerce_str(branch).strip()
        cache_key = _file_404_cache_key(
            _token_digest(headers), owner, name, _branch_cache_token(requested_branch), path
        )
        rc = None
        if cacheable:
            rc = await _get_redis()
            if rc is not None:
                try:
                    if await rc.get(cache_key):
                        return f"File not found: {path} in {repo} (branch: {requested_branch or 'default'})."
                except Exception as exc:
                    logger.debug("get_github_file cache check failed: %s", format_exception_message(exc))

        async with egress_client(timeout=30) as client:
            branch, branch_err = await _resolve_branch(client, headers, repo, owner, name, branch)
            if branch_err:
                return branch_err

            # Canonical "file not found" message — no "Error:" prefix so the
            # agent loop does not log it at WARNING level.  The message is still
            # descriptive enough for the LLM to understand the file is absent.
            not_found_msg = f"File not found: {path} in {repo} (branch: {branch})."

            # Pass expected_statuses={404} so safe_request suppresses the
            # WARNING log for missing files.  A 404 is an expected outcome when
            # convention files (AGENTS.md, .github/labels.yml, …) are probed
            # against repositories that don't follow those conventions.
            resp, err = await _common.safe_request(
                client,
                "get",
                contents_url,
                _PROVIDER,
                f"read file {path} in {repo} (branch: {branch})",
                headers=headers,
                params={"ref": branch},
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    # Populate the negative-lookup cache so subsequent runs skip
                    # this probe entirely instead of re-hitting GitHub each time.
                    if cacheable:
                        if rc is None:
                            rc = await _get_redis()
                        if rc is not None:
                            try:
                                await rc.set(cache_key, "1", ex=_FILE_404_CACHE_TTL)
                            except Exception as exc:
                                logger.debug("get_github_file cache write failed: %s", format_exception_message(exc))
                    return not_found_msg
                return err
            data = resp.json()
            if isinstance(data, list):
                entry_names = [
                    str(entry.get("name", "?")) for entry in data[:20] if isinstance(entry, dict)
                ]
                listing = ", ".join(entry_names) if entry_names else "(empty)"
                suffix = f" (and {len(data) - 20} more)" if len(data) > 20 else ""
                return (
                    f"{path} is a directory, not a file. Contents: "
                    f"{listing}{suffix}. Use a specific file path instead."
                )
            if data.get("type") != "file":
                return f"{path} is not a file (type: {data.get('type')})."
            links = _file_links_text(data)
            download_hint = (
                f"download_github_file(repo='{repo}', path='{path}', branch='{branch}') "
                "brings the file into the conversation (a download card and a file_id)."
            )
            if "content" not in data or data.get("encoding") != "base64":
                return (
                    f"File {path} is too large for inline retrieval "
                    f"(size: {data.get('size', '?')} bytes). "
                    f"Use the GitHub web UI or clone the repository to read it, or "
                    f"{download_hint}{links}"
                )
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                return (
                    f"File {path} appears to be binary "
                    f"(size: {data.get('size', 0)} bytes, branch: {branch}). "
                    f"Cannot display binary file contents: {download_hint}{links}"
                )
            return _file_window(path, branch, content, links, offset=offset, max_chars=max_chars)

    async def download_github_file(repo: str, path: str, branch: str = "") -> str | ToolResult:
        """Download a repository file's raw bytes into the conversation (any type, up to 100 MB)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        path = _coerce_str(path).strip()
        if not path:
            return "Error: 'path' is required (file path relative to the repository root)."
        contents_url = f"{_BASE_URL}/repos/{owner}/{name}/contents/{_enc(path)}"
        async with egress_client(timeout=120) as client:
            branch, branch_err = await _resolve_branch(client, headers, repo, owner, name, branch)
            if branch_err:
                return branch_err
            resp, err = await _common.safe_request(
                client,
                "get",
                contents_url,
                _PROVIDER,
                f"read file {path} in {repo} (branch: {branch})",
                headers=headers,
                params={"ref": branch},
            )
            if err:
                return err
            meta = resp.json()
            if not isinstance(meta, dict) or meta.get("type") != "file":
                kind = meta.get("type") if isinstance(meta, dict) else "directory"
                return f"{path} is not a file (type: {kind})."
            size = int(meta.get("size") or 0)
            if size > _MAX_DOWNLOAD_BYTES:
                return (
                    f"File {path} is {round(size / 1024 / 1024, 1)} MB, over the "
                    f"{_MAX_DOWNLOAD_BYTES // 1024 // 1024} MB download limit.{_file_links_text(meta)}"
                )
            if meta.get("encoding") == "base64" and meta.get("content"):
                data = base64.b64decode(meta["content"])
            else:
                raw, err = await _common.safe_request(
                    client,
                    "get",
                    contents_url,
                    _PROVIDER,
                    f"download file {path} in {repo} (branch: {branch})",
                    headers={**headers, "Accept": "application/vnd.github.raw+json"},
                    params={"ref": branch},
                )
                if err:
                    return err
                data = raw.content
        filename = path.rsplit("/", 1)[-1]
        return await deliver_file(
            data=data,
            filename=filename,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            source="GitHub",
            persist_file_fn=persist_file_fn,
            links=_file_links(meta),
            details=[f"{repo} @ {branch}: {path}"],
        )

    async def _narrowed_archive(
        repo: str, owner: str, name: str, ref: str, paths: list[str]
    ) -> tuple[bytes, list[str], int] | str:
        """The tarball at *ref* streamed to a temporary file: whole when it is within the in-memory limit, else
        repacked with what a checkout under *paths* reads (``repository.narrow_archive``). ``(bytes, the files
        left out, their total bytes)``, or the error."""
        import tempfile

        from app.services.sandbox_workspace.repository import ArchiveTooLargeError, narrow_archive

        url = f"{_BASE_URL}/repos/{owner}/{name}/tarball/{_enc(ref)}"
        if err := _common.request_path_error(url):
            return err
        with tempfile.TemporaryFile(prefix="github-archive-") as spool:
            size = 0
            try:
                async with (
                    egress_client(timeout=600, follow_redirects=True) as client,
                    client.stream("GET", url, headers=headers) as resp,
                ):
                    if resp.status_code >= 400:
                        await resp.aread()
                        return f"Error: GitHub answered HTTP {resp.status_code} to download the archive of {repo} at {ref}."
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > _MAX_STREAMED_ARCHIVE_BYTES:
                            return (
                                f"Error: the archive of {repo} at {ref} is over "
                                f"{_MAX_STREAMED_ARCHIVE_BYTES // 1024 // 1024 // 1024} GB, even narrowed to paths."
                            )
                        spool.write(chunk)
            except httpx.HTTPError as exc:
                return f"Error: GitHub could not be reached to download the archive of {repo} at {ref}: {type(exc).__name__}."
            spool.seek(0)
            if size <= _MAX_ARCHIVE_BYTES:
                # Within the limit: the whole archive, as without paths.
                return spool.read(), [], 0
            bound = _narrowed_archive_bound()
            try:
                narrowed = await asyncio.to_thread(
                    narrow_archive,
                    spool,
                    paths,
                    max_bytes=bound,
                    max_output_bytes=bound,
                )
            except ArchiveTooLargeError as exc:
                return f"Error: the archive of {repo} at {ref} was not narrowed: {exc}."
        logger.info(
            "github archive %s@%s narrowed to %s: %d bytes streamed, %d kept, %d left out",
            repo,
            ref,
            paths,
            size,
            len(narrowed.data),
            len(narrowed.left_out),
        )
        return narrowed.data, narrowed.left_out, narrowed.left_out_bytes

    async def download_github_repository_archive(
        repo: str, ref: str = "", paths: list[str] | None = None
    ) -> str | ToolResult:
        """A repository's tree at *ref* as one tar.gz archive (what ``materialize_repository`` reads).

        GitHub answers with a redirect to a short-lived codeload URL; httpx
        drops the ``Authorization`` header when it follows a redirect to
        another host, so the token never leaves api.github.com. With *paths*
        the archive is streamed to a temporary file and narrowed to the files
        under them plus the small non-media files a checkout's check
        detection reads, so a repository whose whole archive is over the
        in-memory limit can still be checked out narrowly.
        """
        from app.llm.base import FileRef

        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        wanted = [str(p).strip().strip("/") for p in paths or [] if str(p).strip().strip("/")]
        if wanted:
            async with egress_client(timeout=30) as client:
                resolved, ref_err = await _resolve_branch(client, headers, repo, owner, name, ref)
            if ref_err or not resolved:
                return ref_err or f"Error: the ref of {repo} could not be resolved."
            ref = resolved
            narrowed = await _narrowed_archive(repo, owner, name, ref, wanted)
            if isinstance(narrowed, str):
                return narrowed
            data, left_out, left_out_bytes = narrowed
            note = (
                f" The whole archive is over {_MAX_ARCHIVE_BYTES // 1024 // 1024} MB: narrowed to {', '.join(wanted)}; "
                f"{len(left_out)} media, large or over-budget file(s) outside them left out "
                f"({round(left_out_bytes / 1024 / 1024, 1)} MB): {', '.join(left_out[:5])}"
                + (f" and {len(left_out) - 5} more" if len(left_out) > 5 else "")
                + "."
                if left_out
                else ""
            )
            return await _archive_result(data, repo, name, ref, FileRef, note=note, left_out=left_out)
        async with egress_client(timeout=300) as client:
            ref, ref_err = await _resolve_branch(client, headers, repo, owner, name, ref)
            if ref_err:
                return ref_err
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/tarball/{_enc(ref)}",
                _PROVIDER,
                f"download the archive of {repo} at {ref}",
                headers=headers,
                follow_redirects=True,
            )
            if err:
                return err
            data = resp.content
        if len(data) > _MAX_ARCHIVE_BYTES:
            return (
                f"Error: the archive of {repo} at {ref} is {round(len(data) / 1024 / 1024, 1)} MB, over the "
                f"{_MAX_ARCHIVE_BYTES // 1024 // 1024} MB limit: pass paths (materialize_repository: sub_paths) "
                "to keep only the directories and files you need."
            )
        return await _archive_result(data, repo, name, ref, FileRef)

    async def _archive_result(
        data: bytes,
        repo: str,
        name: str,
        ref: str,
        FileRef: Any,  # noqa: N803
        *,
        note: str = "",
        left_out: list[str] | None = None,
    ) -> str | ToolResult:
        filename = f"{name}-{ref.replace('/', '-')}.tar.gz"
        if workspace_ingest_requested():
            # Read by ``materialize_repository`` (or written by
            # ``to_workspace_path``): kept in memory, never a conversation file.
            # ``structured.left_out`` names the files a narrowed archive dropped,
            # so the checkout's answer lists them too.
            ref_file = FileRef.from_bytes(data=data, filename=filename, content_type="application/gzip")
            register_fetched_file(None, ref_file)
            return ToolResult(
                tool_call_id="",
                content=f"Archive of {repo} at {ref}: {round(len(data) / 1024, 1)} KB.{note}",
                files=[ref_file],
                structured={"left_out": list(left_out or [])},
            )
        return await deliver_file(
            data=data,
            filename=filename,
            content_type="application/gzip",
            source="GitHub",
            persist_file_fn=persist_file_fn,
            details=[f"{repo} @ {ref}: the repository's tree (no .git history){note}"],
        )

    async def get_github_repository_tree(repo: str, ref: str = "", path: str = "", count: int = 200) -> str:
        """List a repository's file tree (recursively) for a branch/tag/SHA.

        Returns paths + types so you can discover the layout without reading
        every file. ``ref`` defaults to the repository's default branch; ``path``
        narrows the listing to a sub-directory prefix.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            tree_ref, ref_err = await _resolve_branch(client, headers, repo, owner, name, ref)
            if ref_err:
                return ref_err
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/git/trees/{_enc(tree_ref)}",
                _PROVIDER,
                f"get tree for {tree_ref} in {repo}",
                headers=headers,
                params={"recursive": "1"},
            )
            if err:
                return err
            data = resp.json()
            entries = data.get("tree", [])
            prefix = _coerce_str(path).strip().strip("/")
            if prefix:
                entries = [e for e in entries if str(e.get("path", "")).startswith(prefix)]
            if not entries:
                return f"No tree entries found for {tree_ref} in {repo}" + (f" under '{prefix}'" if prefix else "") + "."
            # Same tool-test-endpoint exposure as ref/path above: a non-int
            # count (null, string) must not crash min()/max() - coerce first.
            raw_count = _coerce_int(count)
            limit = max(1, min(raw_count if raw_count is not None else 200, 500))
            shown = entries[:limit]
            lines = [
                f"Tree for {repo}@{tree_ref}"
                + (f" under '{prefix}'" if prefix else "")
                + f" ({len(entries)} entries"
                + (" — TRUNCATED by GitHub" if data.get("truncated") else "")
                + f", showing {len(shown)}):"
            ]
            for e in shown:
                icon = "📁" if e.get("type") == "tree" else "📄"
                lines.append(f"  {icon} {e.get('path')}")
            if len(entries) > limit:
                lines.append(f"... and {len(entries) - limit} more. Narrow with the 'path' prefix.")
            return "\n".join(lines)

    async def create_or_update_github_file(
        repo: str,
        path: str,
        content: str | None = None,
        message: str = "",
        branch: str = "",
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Create or update a file in a repository: one commit GitHub builds and signs (#12051).

        The write goes through the ``create_github_commit`` path
        (``createCommitOnBranch``, chained on the tip it read), under the
        branch's write lock, so parallel writes on one branch land one after
        the other, each on the tip the previous one left. Only the first
        commit of an empty repository goes through the Contents API, which
        the result says is not signed. The file is ``content`` (text) or
        ``file_id``, any file of the run committed byte-for-byte.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if content is None and not (file_id or "").strip():
            # An omitted source must never commit an empty file over the path;
            # an explicit empty ``content`` still writes an empty file.
            return ToolResult(
                tool_call_id="",
                content="Error: provide content (the file text) or file_id (a file of this conversation).",
                is_error=True,
            )
        if (file_id or "").strip():
            if content is not None:
                return ToolResult(
                    tool_call_id="",
                    content="Error: provide either content (text) or file_id (a file of this conversation), not both.",
                    is_error=True,
                )
            piped = await read_pipe_file(
                file_context, file_id, max_bytes=_MAX_COMMIT_BYTES, target="GitHub"
            )
            if isinstance(piped, str):
                return ToolResult(tool_call_id="", content=piped, is_error=True)
            raw_bytes = piped[0]
        else:
            raw_bytes = _coerce_str(content).encode("utf-8")
        clean_path = _coerce_str(path).strip().strip("/")
        async with branch_write_lock(repo, branch):
            source: dict[str, Any] = {"path": clean_path}
            if (file_id or "").strip():
                source["file_id"] = file_id
            else:
                source["content"] = _coerce_str(content)
            committed = await _create_github_commit(
                repo=repo,
                message=_normalize_commit_message(message, clean_path, "write"),
                files=[source],
                branch=branch,
                file_context=file_context,
            )
            text = committed.content if isinstance(committed, ToolResult) else str(committed)
            if _EMPTY_REPOSITORY_MARK in text:
                return await _put_contents(repo, owner, name, path, raw_bytes, message, branch)
        return _per_file_answer(committed, owner, name, clean_path, "write")

    def _per_file_answer(committed: str | ToolResult, owner: str, name: str, path: str, kind: str) -> str | ToolResult:
        """The answer of a per-file write from the ``create_github_commit`` one it made.

        A landed commit reads as before (``Created``/``Updated``/``Deleted``
        *path* on branch *b* (commit: sha8)) followed by the commit's own
        lines (signature verdict, pull request named); anything else (an
        error, no change) is the commit's answer as it is.
        """
        text = committed.content if isinstance(committed, ToolResult) else str(committed)
        first, _, rest = text.partition("\n")
        landed = _COMMITTED_ON_RE.match(first)
        if not landed:
            return committed
        branch, sha = landed.group(1), landed.group(2)
        # The "Added: x" / "Updated: x" / "Deleted: x" listing of one path says nothing more here.
        tail = [
            line for line in rest.splitlines() if line.strip() and not line.startswith(("Added", "Updated", "Deleted"))
        ]
        if kind == "delete":
            head = f"Deleted {path} from branch {branch} (commit: {sha[:8]})"
        else:
            action = "Updated" if "updated" in first.rsplit(":", 1)[-1] else "Created"
            head = f"{action} {path} on branch {branch} (commit: {sha[:8]})"
            web = f"https://github.com/{owner}/{name}/blob/{_enc(branch)}/{_enc(path)}"
            raw = f"https://raw.githubusercontent.com/{owner}/{name}/{_enc(branch)}/{_enc(path)}"
            tail = [f"Web URL: {web}", f"Download URL: {raw}", *tail]
        return "\n".join([head, *tail])

    async def _put_contents(
        repo: str, owner: str, name: str, path: str, raw_bytes: bytes, message: str, branch: str
    ) -> str | ToolResult:
        """The first commit of an empty repository, through the Contents API (no tip to build a signed one on)."""
        encoded = base64.b64encode(raw_bytes).decode("ascii")
        contents_url = f"{_BASE_URL}/repos/{owner}/{name}/contents/{_enc(path)}"
        async with egress_client(timeout=60) as client:
            branch, branch_err = await _resolve_branch(client, headers, repo, owner, name, branch)
            if branch_err:
                return branch_err
            # A 404 miss is the expected "file does not exist yet" signal for
            # the create path; any other failure (401/403/5xx, network error)
            # must surface immediately instead of degrading into a sha-less
            # PUT that would fail with a misleading secondary error.
            existing_resp, existing_err = await _common.safe_request(
                client,
                "get",
                contents_url,
                _PROVIDER,
                f"check if {path} exists in {repo}",
                headers=headers,
                params={"ref": branch},
                expected_statuses={404},
            )
            if existing_err and (existing_resp is None or existing_resp.status_code != 404):
                return existing_err
            payload: dict[str, Any] = {"content": encoded, "branch": branch}
            is_update = False
            if existing_err is None and existing_resp is not None:
                data = existing_resp.json()
                if isinstance(data, dict) and "sha" in data:
                    payload["sha"] = data["sha"]
                    is_update = True
            payload["message"] = _normalize_commit_message(message, path, "update" if is_update else "create")
            resp, err = await _common.safe_request(
                client,
                "put",
                contents_url,
                _PROVIDER,
                f"{'update' if is_update else 'create'} {path} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            result = resp.json()
            action = "Updated" if is_update else "Created"
            commit_sha = result.get("commit", {}).get("sha", "?")[:8]
            await _forget_cached_404s(headers, owner, name, branch, [path])
            committed = result.get("content") if isinstance(result.get("content"), dict) else {}
            commit_info = result.get("commit") if isinstance(result.get("commit"), dict) else {}
            signing = _contents_signing_note(commit_info, branch)
        named = await name_pushed_pull_request(repo, owner, name, branch, _coerce_str(commit_info.get("sha")))
        tail = f"\n{named}" if named else ""
        return f"{action} {path} on branch {branch} (commit: {commit_sha}){_file_links_text(committed)}{signing}{tail}"

    async def create_github_commit(
        repo: str,
        message: str,
        files: list[dict[str, Any]] | None = None,
        delete_paths: list[str] | None = None,
        branch: str = "",
        from_checkout: str = "",
        paths: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Commit several file changes to one branch as a single signed commit, under the branch's write lock.

        Parallel calls on one repository branch (this tool and the per-file
        ones) run one after the other, each on the tip the previous one left
        (#12051): three writes in one round land as three commits in call
        order instead of one landing and the others refused on a stale tip.
        """
        async with branch_write_lock(repo, branch):
            return await _create_github_commit(
                repo=repo,
                message=message,
                files=files,
                delete_paths=delete_paths,
                branch=branch,
                from_checkout=from_checkout,
                paths=paths,
                file_context=file_context,
            )

    async def _create_github_commit(
        repo: str,
        message: str,
        files: list[dict[str, Any]] | None = None,
        delete_paths: list[str] | None = None,
        branch: str = "",
        from_checkout: str = "",
        paths: list[str] | None = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Commit several file changes to one branch as a single commit.

        The checks are shared: read the branch tip and its tree, look up what
        each path already is, and build ONE tree on top of the tip (contents
        inline, deletions as ``sha: null``), which tells apart a change the
        branch already has. With no file mode involved, GraphQL
        ``createCommitOnBranch`` then makes the commit: GitHub builds and signs
        it, and moves the branch only while it still points at the tip read.
        Otherwise - or when GitHub will not build it (too large, mutation not
        offered) - the Git Data API commits that tree and fast-forwards the
        branch once its tip is confirmed unchanged. Either way the result
        reports GitHub's own verdict on the commit's signature, and the branch
        moves once, so a change set never lands half-applied.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        checkout_note = ""
        checkout_record: Any = None
        #: Workspace files a merge with the branch changed: written back when the record moves.
        checkout_rewrites: dict[str, bytes] = {}
        #: The tip the change set was merged against: the commit must land on it, or not at all.
        merged_on = ""
        #: The branch changed files of the checkout this commit does not carry: the record keeps its commit.
        keep_record_commit = False
        if str(from_checkout or "").strip():
            # The change set is what the sandbox workspace changed since the
            # checkout record materialize_repository wrote:
            # modified and added files as workspace bytes, removed files as
            # deletions. 'files' and 'delete_paths' are then not accepted: one
            # commit has one source of truth.
            if files or delete_paths:
                return "Error: give either from_checkout or files/delete_paths, not both."
            loaded = await _changes_from_checkout(from_checkout, paths, repo)
            if isinstance(loaded, str):
                return loaded
            files, delete_paths, checkout_note, checkout_record = loaded
            adopted_note = await _adopt_landed_commit(headers, repo, owner, name, branch, checkout_record)
            if adopted_note:
                # An earlier call's commit had landed: the change set is read again from the moved record.
                loaded = await _changes_from_checkout(from_checkout, paths, repo)
                if isinstance(loaded, str):
                    return loaded
                files, delete_paths, checkout_note, checkout_record = loaded
                checkout_note = f"{adopted_note} {checkout_note}"
            if not files and not delete_paths:
                return checkout_note
            drift = await _reconcile_checkout_drift(
                headers, repo, owner, name, branch, from_checkout, checkout_record, files, delete_paths
            )
            if isinstance(drift, str):
                return drift
            files, delete_paths, drift_note, checkout_rewrites, merged_on, keep_record_commit = drift
            if drift_note:
                checkout_note = f"{checkout_note} {drift_note}"
            if not files and not delete_paths:
                return checkout_note
            sizes = {str(item["path"]): len(item["content"].data) for item in files}
            over_count = len(files) + len(delete_paths or []) > _MAX_COMMIT_CHANGES
            if over_count or sum(sizes.values()) > _MAX_COMMIT_CONTENT_BYTES:
                return await _commit_checkout_in_batches(
                    repo, message, files, list(delete_paths or []), branch, checkout_note, checkout_record,
                    checkout_rewrites, keep_record_commit=keep_record_commit,
                )
        parsed, deletes, change_err = _parse_commit_changes(files, delete_paths)
        if change_err:
            return change_err
        #: The writes as given; an ``_Edits`` one is resolved against the branch tip below.
        staged: list[tuple[str, str | bytes | _Edits, bool | None]] = []
        #: Workspace path -> (repository path, bytes) of each files[] write a ``workspace_path`` read:
        #: the checkout record holding it moves with the commit (``_advance_checkout_files``).
        workspace_written: dict[str, tuple[str, bytes]] = {}
        #: What the merge of those checkout files with a moved branch did (``_reconcile_workspace_writes``).
        files_drift_note = ""
        total_bytes = 0
        for path, content, executable in parsed:
            if isinstance(content, _Edits):
                staged.append((path, content, executable))
                continue
            if isinstance(content, _FileRef):
                piped = await read_pipe_file(
                    file_context, content.file_id, max_bytes=_MAX_COMMIT_CONTENT_BYTES, target="a GitHub commit"
                )
                if isinstance(piped, str):
                    return piped
                parked = file_context.get(content.file_id) if file_context is not None else None
                source_path = getattr(parked, "workspace_path", None)
                if isinstance(source_path, str) and source_path and not checkout_record:
                    workspace_written[source_path] = (path, bytes(piped[0]))
                content = piped[0]
            elif isinstance(content, _WorkspaceBytes):
                content = content.data
            total_bytes += len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
            staged.append((path, content, executable))
        if workspace_written:
            # A checkout file read whole: merged with what the branch gained since the checkout, never written over it.
            reconciled = await _reconcile_workspace_writes(headers, repo, owner, name, branch, workspace_written)
            if isinstance(reconciled, str):
                return reconciled
            replaced, drift_note, merged_on = reconciled
            if replaced:
                staged = [(p, replaced.get(p, c) if not isinstance(c, _Edits) else c, x) for p, c, x in staged]
                total_bytes = sum(
                    len(c) if isinstance(c, bytes) else len(c.encode("utf-8"))
                    for _p, c, _x in staged
                    if not isinstance(c, _Edits)
                )
            files_drift_note = drift_note
        if total_bytes > _MAX_COMMIT_CONTENT_BYTES and not any(isinstance(c, _Edits) for _p, c, _x in staged):
            # An 'edits' entry is resolved at the branch tip first: a set holding
            # one is split below, once every file is known whole (#1307).
            split = _split_files_change_set(staged, total_bytes)
            if isinstance(split, str):
                return split
            # Over the cap of one signed commit: the from_checkout split path (#764),
            # consecutive signed commits on a scratch branch and one branch move.
            return await _commit_checkout_in_batches(
                repo,
                message,
                split,
                list(deletes),
                branch,
                files_drift_note,
                None,
                workspace_written=workspace_written,
                merged_on=merged_on,
            )
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=60) as client:
            branch, branch_err = await _resolve_branch(client, headers, repo, owner, name, branch)
            if branch_err:
                return branch_err
            branch_err = _branch_name_error(branch)
            if branch_err:
                return branch_err
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{repo_url}/git/ref/heads/{_enc(branch)}",
                _PROVIDER,
                f"read branch {branch} of {repo}",
                headers=headers,
                expected_statuses={404, 409},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return f"Error: branch '{branch}' does not exist in {repo}. Create it with create_github_branch first."
                if resp is not None and resp.status_code == 409 and "empty" in _coerce_str(
                    _json_object(resp).get("message")
                ).lower():
                    # The Git Data API cannot build on a repository with no
                    # commit; the contents API can make the first one.
                    return (
                        f"Error: {repo} is empty (it has no commit yet), so there is nothing to commit on top of. "
                        "Create its first file with create_or_update_github_file, then call this tool again."
                    )
                return err
            ref_object = _json_object(resp).get("object")
            tip = _coerce_str(ref_object.get("sha")) if isinstance(ref_object, dict) else ""
            if not _GIT_OBJECT_ID_RE.fullmatch(tip):
                return f"Error: GitHub did not return the tip commit of branch '{branch}' in {repo}."
            if merged_on and tip != merged_on:
                # The change set was merged with another tip: on this one it could revert a commit.
                return _moved_tip_refusal(branch, repo, tip)
            if checkout_note and not keep_record_commit and not checkout_rewrites:
                # Noted before the commit: a call cut after it landed lets the next one adopt it (#12377).
                await _note_pending_commit(checkout_record, branch, tip, files, delete_paths)
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{repo_url}/git/commits/{tip}",
                _PROVIDER,
                f"read commit {tip[:8]} of {repo}",
                headers=headers,
            )
            if err:
                return err
            tree_ref = _json_object(resp).get("tree")
            base_tree = _coerce_str(tree_ref.get("sha")) if isinstance(tree_ref, dict) else ""
            if not _GIT_OBJECT_ID_RE.fullmatch(base_tree):
                return f"Error: GitHub did not return the tree of commit {tip[:8]} in {repo}."

            listings: dict[str, dict[str, dict[str, Any]]] = {}
            #: Per directory read, the entry names of each case-folded form
            #: (a repository made on Linux can hold 'README.md' and 'readme.md').
            folded_names: dict[str, dict[str, list[str]]] = {}

            async def listing(tree_sha: str) -> tuple[dict[str, dict[str, Any]], str | None]:
                """The entries of one directory, read once per commit."""
                if tree_sha in listings:
                    return listings[tree_sha], None
                if len(listings) >= _MAX_COMMIT_TREE_READS:
                    return {}, (
                        f"Error: this change touches more than {_MAX_COMMIT_TREE_READS} directories of {repo}; "
                        "split it into several commits."
                    )
                if not _GIT_OBJECT_ID_RE.fullmatch(tree_sha):
                    return {}, f"Error: GitHub returned an unexpected directory id in {repo}."
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{repo_url}/git/trees/{tree_sha}",
                    _PROVIDER,
                    f"read a directory of {repo}",
                    headers=headers,
                )
                if err:
                    return {}, err
                data = _json_object(resp)
                if data.get("truncated") or not isinstance(data.get("tree"), list):
                    return {}, f"Error: GitHub did not list a directory of {repo} in full, so nothing was committed."
                entries = {
                    entry["path"]: entry
                    for entry in data["tree"]
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                }
                listings[tree_sha] = entries
                folded: dict[str, list[str]] = {}
                for entry_name in entries:
                    folded.setdefault(entry_name.casefold(), []).append(entry_name)
                folded_names[tree_sha] = folded
                return entries, None

            async def entry_at(path: str) -> tuple[dict[str, Any] | None, str | None, list[str], str | None]:
                """``(entry, blocking file, case twins, error)`` for *path* on the branch now.

                ``entry`` is ``None`` when the path does not exist; the
                blocking file is an ancestor that is a file, not a directory;
                the case twins are the existing paths that differ from the
                first missing part of *path* only in case.
                """
                segments = path.split("/")
                tree_sha = base_tree
                for depth, segment in enumerate(segments):
                    entries, err = await listing(tree_sha)
                    if err:
                        return None, None, [], err
                    entry = entries.get(segment)
                    if entry is None:
                        twins = folded_names[tree_sha].get(segment.casefold(), [])
                        return None, None, ["/".join([*segments[:depth], twin]) for twin in twins], None
                    if depth == len(segments) - 1:
                        return entry, None, [], None
                    if entry.get("type") != "tree":
                        return None, "/".join(segments[: depth + 1]), [], None
                    tree_sha = _coerce_str(entry.get("sha"))
                return None, None, [], None

            async def edited(path: str, entry: dict[str, Any], change: _Edits) -> tuple[str, str | None]:
                """``(new text, error)``: the file at the tip with *change* applied, all or nothing."""
                size = _coerce_int(entry.get("size")) or 0
                if size > _MAX_COMMIT_CONTENT_BYTES:
                    return "", (
                        f"Error: '{path}' is {size} bytes on branch '{branch}' of {repo}, over the "
                        f"{_MAX_COMMIT_CONTENT_BYTES} one commit may carry; nothing was committed."
                    )
                blob_sha = _coerce_str(entry.get("sha"))
                if not _GIT_OBJECT_ID_RE.fullmatch(blob_sha):
                    return "", f"Error: GitHub returned an unexpected id for '{path}' in {repo}; nothing was committed."
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{repo_url}/git/blobs/{blob_sha}",
                    _PROVIDER,
                    f"read '{path}' of {repo}",
                    headers=headers,
                )
                if err:
                    return "", f"{err}\nNothing was committed to branch '{branch}' of {repo}."
                blob = _json_object(resp)
                try:
                    raw = base64.b64decode(_coerce_str(blob.get("content")))
                    before = raw.decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    return "", (
                        f"Error: '{path}' on branch '{branch}' of {repo} is not UTF-8 text, so it cannot be edited by "
                        "find/replace; give its full 'content' or a 'file_id'. Nothing was committed."
                    )
                try:
                    applied = editing.apply_edits(before, change.edits, read_with="get_github_file")
                except editing.EditError as exc:
                    return "", f"Error: '{path}' on branch '{branch}' of {repo}: {exc} The branch is unchanged."
                diffs.append(editing.unified_diff(path, before, applied.text) or f"(no change in {path})\n")
                return applied.text, None

            tree_entries: list[dict[str, Any]] = []
            added: list[str] = []
            updated: list[str] = []
            #: The read-back of each file changed by 'edits', in order.
            diffs: list[str] = []
            writes: list[tuple[str, str | bytes, bool | None]] = []
            #: Why a file mode is involved, per write that involves one.
            mode_reasons: list[str] = []
            for path, given, executable in staged:
                entry, blocking, twins, err = await entry_at(path)
                if err:
                    return err
                if blocking:
                    return f"Error: '{blocking}' is a file in {repo}, so '{path}' cannot be written inside it."
                if isinstance(given, _Edits):
                    if entry is None:
                        return (
                            f"Error: '{path}' does not exist on branch '{branch}' of {repo}, so it cannot be edited; "
                            "give its full 'content' to create it. Nothing was committed."
                        )
                    refusal = _unwritable(entry, path, repo)
                    if refusal:
                        return refusal
                    text, err = await edited(path, entry, given)
                    if err:
                        return err
                    total_bytes += len(text.encode("utf-8"))
                    written: str | bytes = text
                else:
                    written = given
                writes.append((path, written, executable))
                if total_bytes > _MAX_COMMIT_CONTENT_BYTES:
                    # Over the cap of one signed commit: the files are resolved
                    # here and committed by the split path below; each part
                    # runs the per-file checks again on the scratch branch.
                    continue
                #: The spellings this write renames by case, with their modes.
                renamed: list[tuple[str, str]] = []
                for twin in twins:
                    # Read from the listings already made: no request.
                    twin_entry, _, _, err = await entry_at(twin)
                    if err:
                        return err
                    is_directory = twin_entry is not None and twin_entry.get("type") == "tree"
                    if twin in deletes and not is_directory:
                        # A case-only rename: the old spelling leaves the tree in this commit.
                        renamed.append((twin, _coerce_str((twin_entry or {}).get("mode"))))
                        continue
                    new = "/".join(path.split("/")[: twin.count("/") + 1])
                    where = f"'{new}'" if new == path else f"'{new}' (in '{path}')"
                    collision = (
                        f"Error: {where} differs only in case from {'the directory ' if is_directory else ''}"
                        f"'{twin}' on branch '{branch}' of {repo}, and the two collide on a case-insensitive "
                        "checkout (Windows, macOS). Use the existing spelling"
                    )
                    if is_directory:
                        return (
                            f"{collision}, or rename the directory in two commits: delete the files under '{twin}', "
                            "then write them under the new name."
                        )
                    return f"{collision}, or delete '{twin}' in the same commit to rename it."
                #: The renamed spelling whose executable bit the new one keeps.
                inherited = ""
                if entry is None and executable is None and renamed:
                    scripts = [twin for twin, twin_mode in renamed if twin_mode == _MODE_EXECUTABLE]
                    if scripts and len(scripts) < len(renamed):
                        listed = ", ".join(f"'{twin}'" for twin, _ in renamed)
                        return (
                            f"Error: '{path}' renames {listed}, which are not all executable; give 'executable' "
                            "(true or false) for it."
                        )
                    inherited = scripts[0] if scripts else ""
                if entry is None:
                    mode = _MODE_EXECUTABLE if executable or inherited else _MODE_FILE
                    added.append(path)
                else:
                    refusal = _unwritable(entry, path, repo)
                    if refusal:
                        return refusal
                    keeps_bit = executable is None and _coerce_str(entry.get("mode")) == _MODE_EXECUTABLE
                    mode = _MODE_EXECUTABLE if executable or keeps_bit else _MODE_FILE
                    updated.append(path)
                if inherited:
                    reason: str | None = f"'{path}' keeps the executable bit of '{inherited}'"
                else:
                    reason = _mode_reason(path, None if entry is None else _coerce_str(entry.get("mode")), mode)
                if reason:
                    mode_reasons.append(reason)
                tree_entry: dict[str, Any] = {"path": path, "mode": mode, "type": "blob"}
                if isinstance(written, bytes):
                    # Inline tree content must be text: a file goes up as a blob first.
                    resp, err = await _common.safe_request(
                        client,
                        "post",
                        f"{repo_url}/git/blobs",
                        _PROVIDER,
                        f"upload '{path}' to {repo}",
                        headers=_json_headers(headers),
                        json={"content": base64.b64encode(written).decode("ascii"), "encoding": "base64"},
                    )
                    if err:
                        return f"{err}\nNothing was committed to branch '{branch}' of {repo}."
                    blob_sha = _coerce_str(_json_object(resp).get("sha"))
                    if not _GIT_OBJECT_ID_RE.fullmatch(blob_sha):
                        return f"Error: GitHub did not return the uploaded '{path}' in {repo}; nothing was committed."
                    tree_entry["sha"] = blob_sha
                else:
                    tree_entry["content"] = written
                tree_entries.append(tree_entry)
            if total_bytes > _MAX_COMMIT_CONTENT_BYTES:
                # 'edits' entries resolved whole at this tip: the change set goes
                # through the split path as plain files, refused there if the
                # branch moved past this tip meanwhile (#1307).
                split = _split_files_change_set(writes, total_bytes)
                if isinstance(split, str):
                    return split
                answer = await _commit_checkout_in_batches(
                    repo,
                    message,
                    split,
                    list(deletes),
                    branch,
                    files_drift_note,
                    None,
                    workspace_written=workspace_written,
                    merged_on=tip,
                )
                if diffs:
                    answer = f"{answer}\nEdits applied:\n" + "".join(diffs).rstrip("\n")
                return answer
            for path in deletes:
                entry, blocking, twins, err = await entry_at(path)
                if err:
                    return err
                if entry is None or blocking:
                    near = f" (it has '{twins[0]}', which differs only in case)" if twins else ""
                    return (
                        f"Error: '{path}' does not exist on branch '{branch}' of {repo}{near}, so it cannot be "
                        "deleted."
                    )
                refusal = _undeletable(entry, path, repo)
                if refusal:
                    return refusal
                current = _coerce_str(entry.get("mode"))
                mode = current if current in (_MODE_EXECUTABLE, _MODE_SYMLINK) else _MODE_FILE
                tree_entries.append({"path": path, "mode": mode, "type": "blob", "sha": None})

            resp, err = await _common.safe_request(
                client,
                "post",
                f"{repo_url}/git/trees",
                _PROVIDER,
                f"build the tree of a commit to {repo}",
                headers=_json_headers(headers),
                json={"base_tree": base_tree, "tree": tree_entries},
            )
            if err:
                return err
            new_tree = _coerce_str(_json_object(resp).get("sha"))
            if not _GIT_OBJECT_ID_RE.fullmatch(new_tree):
                return f"Error: GitHub did not return the new tree for {repo}; nothing was committed."
            if new_tree == base_tree:
                # The tree was built on the tip read first: "already has this
                # content" holds only while the branch still points at it.
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{repo_url}/git/ref/heads/{_enc(branch)}",
                    _PROVIDER,
                    f"re-read branch {branch} of {repo}",
                    headers=headers,
                )
                if err:
                    return f"{err}\nNothing was committed to branch '{branch}' of {repo}."
                current_object = _json_object(resp).get("object")
                current = _coerce_str(current_object.get("sha")) if isinstance(current_object, dict) else ""
                if current != tip:
                    return _moved_tip_refusal(branch, repo, current)
                # Every written path exists with this content, so a cached 404
                # for one of them is stale all the same.
                await _forget_cached_404s(headers, owner, name, branch, [path for path, _, _ in writes])
                no_change = f"No change: branch '{branch}' of {repo} already has this content, so nothing was committed."
                # The branch already holds the checkout's change set: the
                # record moves to its tip, so it is no longer a change.
                moved = await _advance_checkout(
                    checkout_record, "" if keep_record_commit else tip, branch, files, delete_paths, checkout_rewrites
                ) or await _advance_checkout_files(
                    repo, branch, tip, tip, workspace_written, [p for p, _, _ in writes] + list(deletes)
                )
                return ToolResult(
                    tool_call_id="",
                    content=f"{no_change}\n{moved}" if moved else no_change,
                    no_op=True,
                )
            change_count = len(tree_entries)
            commit_message = _normalize_commit_message(
                message, f"{change_count} file" if change_count == 1 else f"{change_count} files", "update"
            )
            unsigned_reason = ""
            #: What can still be committed signed when the branch refuses an unsigned commit.
            signed_alternative = ""
            if mode_reasons:
                more = len(mode_reasons) - 1
                unsigned_reason = mode_reasons[0] + (f" (and {more} more)" if more > 0 else "")
                signed_alternative = (
                    "A change that sets or clears an executable bit is not signed with this integration's "
                    "credential: commit the other files without it (GitHub then builds and signs the commit) and "
                    "leave that change to someone who can sign it."
                )
            signing_note = ""
            new_commit = ""
            if not unsigned_reason:
                # No file mode is involved: GitHub builds and signs the commit,
                # moving the branch only if it still points at the tip read.
                headline, _, body = commit_message.partition("\n")
                message_input: dict[str, str] = {"headline": headline}
                if body.strip():
                    message_input["body"] = body.strip("\n")
                file_changes: dict[str, Any] = {}
                if writes:
                    file_changes["additions"] = [
                        {
                            "path": path,
                            "contents": base64.b64encode(
                                content if isinstance(content, bytes) else content.encode("utf-8")
                            ).decode("ascii"),
                        }
                        for path, content, _ in writes
                    ]
                if deletes:
                    file_changes["deletions"] = [{"path": path} for path in deletes]
                commit_input = {
                    "branch": {"repositoryNameWithOwner": f"{owner}/{name}", "branchName": branch},
                    "expectedHeadOid": tip,
                    "message": message_input,
                    "fileChanges": file_changes,
                }
                unknown = (
                    f"\nWhether branch '{branch}' of {repo} now carries this change is not known. "
                    + _retry_note("the commit landed", deletes)
                )
                retried = False
                while True:
                    resp, err = await _common.safe_request(
                        client,
                        "post",
                        _GRAPHQL_URL,
                        _PROVIDER,
                        f"commit to branch {branch} of {repo}",
                        headers=_json_headers(headers),
                        json={"query": _CREATE_COMMIT_MUTATION, "variables": {"input": commit_input}},
                        expected_statuses=set(),
                    )
                    # expectedHeadOid makes a second attempt safe: a first one
                    # that landed moved the branch, so the second is refused.
                    if retried or not _transient_commit_failure(resp, err):
                        break
                    retried = True
                if err and resp is not None and resp.status_code == 413:
                    unsigned_reason = "GitHub refused the change as too large for a commit it builds itself"
                    signed_alternative = "Split the change into smaller commits, which GitHub builds and signs itself."
                elif err and (resp is None or _answer_unknown_status(resp.status_code)):
                    return f"{err}{unknown}"
                elif err:
                    return f"{err}\nBranch '{branch}' of {repo} is unchanged."
                else:
                    answer = _json_object(resp)
                    errors = answer.get("errors")
                    has_errors = isinstance(errors, list) and bool(errors)
                    data = answer.get("data")
                    created = data.get("createCommitOnBranch") if isinstance(data, dict) else None
                    commit_ref = created.get("commit") if isinstance(created, dict) else None
                    new_commit = _coerce_str(commit_ref.get("oid")) if isinstance(commit_ref, dict) else ""
                    if isinstance(commit_ref, dict) and _GIT_OBJECT_ID_RE.fullmatch(new_commit):
                        # A commit came back: it landed, whatever else failed
                        # in the answer (resolving its signature, say).
                        verdict = _graphql_signature_verdict(commit_ref)
                        if has_errors and verdict is not None and not verdict.signed:
                            # A null signature next to an error is that error, not an unsigned commit.
                            verdict = None
                        signing_note = _graphql_signing_note(verdict)
                    elif has_errors:
                        new_commit = ""
                        error_text, kinds = _graphql_error_text(errors)
                        if retried and _head_moved(error_text):
                            # The first attempt may have landed and moved the branch itself.
                            return f"Error: GitHub did not answer the first attempt clearly for {repo}.{unknown}"
                        if "FORBIDDEN" in kinds and not _head_moved(error_text):
                            # GitHub answers FORBIDDEN for a stale expectedHeadOid too
                            # (a resumed call on a branch that moved): the tip says which.
                            now_tip = await _read_branch_tip(client, repo_url, headers, branch, repo)
                            if now_tip and now_tip != tip:
                                if retried:
                                    return (
                                        f"Error: GitHub did not answer the first attempt clearly for {repo}.{unknown}"
                                    )
                                error_text = f"Expected branch to point to {tip} ({error_text})"
                        if not _signing_unavailable(error_text, kinds):
                            refusal = _graphql_commit_refusal(error_text, kinds, branch, repo, tip)
                            return f"{refusal}{_NO_UNSIGNED_FALLBACK}"
                        unsigned_reason = "this GitHub server does not offer createCommitOnBranch, the call GitHub signs"
                    else:
                        return f"Error: GitHub did not return the new commit for {repo}.{unknown}"
            if not new_commit:
                path_note = f"\n{_git_data_path_note(unsigned_reason)}"
                resp, err = await _common.safe_request(
                    client,
                    "post",
                    f"{repo_url}/git/commits",
                    _PROVIDER,
                    f"create a commit in {repo}",
                    headers=_json_headers(headers),
                    json={"message": commit_message, "tree": new_tree, "parents": [tip]},
                )
                if err:
                    # Only the ref move below moves the branch.
                    return f"{err}\nBranch '{branch}' of {repo} is unchanged.{path_note}"
                created_commit = _json_object(resp)
                new_commit = _coerce_str(created_commit.get("sha"))
                if not _GIT_OBJECT_ID_RE.fullmatch(new_commit):
                    return (
                        f"Error: GitHub did not return the new commit for {repo}; branch '{branch}' is "
                        f"unchanged.{path_note}"
                    )
                signing_note = _git_data_signing_note(unsigned_reason, _rest_signature_verdict(created_commit))
                note = f"\n{signing_note}"
                # The REST refs API takes no expected old value (a git push does: the
                # server compares the ref with the tip it advertised), so the tip is
                # read again right before the move: a push or a force-reset since
                # the tree was built is refused here, and ``force: false`` still
                # refuses a push landing in between. Only a force-reset to an older
                # commit within that last round-trip goes unseen.
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{repo_url}/git/ref/heads/{_enc(branch)}",
                    _PROVIDER,
                    f"re-read branch {branch} of {repo}",
                    headers=headers,
                )
                if err:
                    return (
                        f"{err}\nCommit {new_commit[:8]} was created but branch '{branch}' was not moved to it, so the "
                        f"branch is unchanged.{note}"
                    )
                current_object = _json_object(resp).get("object")
                current = _coerce_str(current_object.get("sha")) if isinstance(current_object, dict) else ""
                if current != tip:
                    return f"{_moved_tip_refusal(branch, repo, current)}{note}"
                resp, err = await _common.safe_request(
                    client,
                    "patch",
                    f"{repo_url}/git/refs/heads/{_enc(branch)}",
                    _PROVIDER,
                    f"move branch {branch} of {repo} to the new commit",
                    headers=_json_headers(headers),
                    json={"sha": new_commit, "force": False},
                    expected_statuses=set(),
                )
                if err:
                    if resp is None or resp.status_code >= 500:
                        # No answer, or a server / gateway error: the move may have
                        # been applied before the answer was lost.
                        return (
                            f"{err}\nCommit {new_commit[:8]} was created, but whether branch '{branch}' now points at "
                            f"it is not known. {_retry_note('the branch did move', deletes)}{note}"
                        )
                    if _requires_signed_commits(resp):
                        # A pull request from another branch would carry the same unsigned commit.
                        return (
                            f"{err}\nCommit {new_commit[:8]} was created but branch '{branch}' was not moved to it, so "
                            f"the branch is unchanged: it requires signed commits, and this one has no verified "
                            f"signature ({unsigned_reason}, so it was built through the Git Data API). "
                            f"{signed_alternative}"
                        ).rstrip()
                    return (
                        f"{err}\nCommit {new_commit[:8]} was created but branch '{branch}' was not moved to it, so the "
                        "branch is unchanged. Either the branch moved while the commit was being built (call the tool "
                        "again to commit on top of the new tip) or it is protected (commit to a branch made with "
                        f"create_github_branch, then open a pull request).{note}"
                    )
        await _forget_cached_404s(headers, owner, name, branch, [path for path, _, _ in writes])
        counts = ", ".join(
            f"{len(paths)} {label}"
            for label, paths in (("added", added), ("updated", updated), ("deleted", deletes))
            if paths
        )
        lines = [
            f"Committed {change_count} file change(s) to branch '{branch}' of {repo} in one commit "
            f"({new_commit}): {counts}."
        ]
        lines += [
            _listed_paths(label, paths)
            for label, paths in (("Added", added), ("Updated", updated), ("Deleted", deletes))
            if paths
        ]
        if checkout_note:
            lines.append(checkout_note)
            moved = await _advance_checkout(
                checkout_record, "" if keep_record_commit else new_commit, branch, files, delete_paths, checkout_rewrites
            )
            if moved:
                lines.append(moved)
        elif workspace_written:
            if files_drift_note:
                lines.append(files_drift_note)
            moved = await _advance_checkout_files(
                repo, branch, tip, new_commit, workspace_written, [p for p, _, _ in writes] + list(deletes)
            )
            if moved:
                lines.append(moved)
        lines.append(signing_note)
        if diffs:
            lines.append("Edits applied:\n" + "".join(diffs).rstrip("\n"))
        named = await name_pushed_pull_request(repo, owner, name, branch, new_commit)
        if named:
            lines.append(named)
        return "\n".join(lines)

    async def _commit_checkout_in_batches(
        repo: str,
        message: str,
        files: list[dict[str, Any]],
        delete_paths: list[str],
        branch: str,
        checkout_note: str,
        checkout_record: Any,
        rewrites: dict[str, bytes] | None = None,
        *,
        keep_record_commit: bool = False,
        workspace_written: dict[str, tuple[str, bytes]] | None = None,
        merged_on: str = "",
    ) -> str:
        """A ``from_checkout`` change set over a cap of one commit: several signed commits, one branch move.

        Over the bytes or over the change count of one commit, the same way.
        The batches (``checkout_commit_plan``: smallest files first, so code
        and tests come before the large catalogs; deletions with the first)
        are committed one after the other with ``createCommitOnBranch`` on a
        scratch branch made at the target's tip, so GitHub builds and signs each one; the target branch is then
        fast-forwarded once to the last of them, while it still points at the
        tip read, and the scratch branch is deleted. The target never points at
        an intermediate commit (CI never starts on a half-applied change set),
        and a batch that fails leaves it unchanged: the change set lands whole
        or not at all.

        *workspace_written* are the checkout files a ``files[]`` set read
        through ``workspace_path`` (#764): their record moves to the last part,
        as the single-commit path does. *merged_on* is the tip those files were
        merged with; a target at another tip is refused before any part.
        """
        owner, name, _ = parse_repo(repo)
        repo_url = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=60) as client:
            target, err = await _resolve_branch(client, headers, repo, owner, name, branch)
            if err or not target:
                return err or f"Error: could not resolve the branch of {repo}; nothing was committed."
            name_err = _branch_name_error(target)
            if name_err:
                return name_err
            resp, err = await _common.safe_request(
                client, "get", f"{repo_url}/git/ref/heads/{_enc(target)}", _PROVIDER,
                f"read branch {target} of {repo}", headers=headers,
            )
            if err:
                return f"{err}\nNothing was committed."
            ref_object = _json_object(resp).get("object")
            tip = _coerce_str(ref_object.get("sha")) if isinstance(ref_object, dict) else ""
            if not _GIT_OBJECT_ID_RE.fullmatch(tip):
                return f"Error: could not read the tip of branch '{target}' of {repo}; nothing was committed."
            if merged_on and tip != merged_on:
                # The checkout files were merged with another tip: on this one they could revert a commit.
                return _moved_tip_refusal(target, repo, tip)
            now = time.time()
            await _sweep_stale_scratch_branches(client, headers, repo_url, repo, now)
            scratch = _scratch_branch_name(tip, now)
            _, err = await _common.safe_request(
                client, "post", f"{repo_url}/git/refs", _PROVIDER, f"create scratch branch {scratch} in {repo}",
                headers=_json_headers(headers), json={"ref": f"refs/heads/{scratch}", "sha": tip},
            )
            if err:
                return f"{err}\nThe scratch branch the split commits on could not be made; nothing was committed."

        async def drop_scratch() -> None:
            try:
                async with egress_client(timeout=30) as drop_client:
                    await _common.safe_request(
                        drop_client, "delete", f"{repo_url}/git/refs/heads/{_enc(scratch)}", _PROVIDER,
                        f"delete scratch branch {scratch} of {repo}", headers=headers,
                    )
            except Exception as exc:  # best effort: the next split sweeps it after a day
                logger.warning("could not delete scratch branch %s of %s: %s", scratch, repo, format_exception_message(exc))

        # The scratch branch is deleted on every way out (a failed part, a refused
        # move, an exception or a cancelled run included) except one: a move the
        # API refused after every part landed, whose signed commits a pull request
        # can still take. That one is swept by the next split on the repository
        # once it is older than _SCRATCH_MAX_AGE_SECONDS.
        keep_scratch = False
        try:
            by_path = {str(item["path"]): item for item in files}
            # Called over a cap only, so there is at least one batch.
            plan = checkout_commit_plan(
                {path: len(item["content"].data) for path, item in by_path.items()}, list(delete_paths)
            )
            count = len(plan)
            landed: list[str] = []
            written: list[dict[str, Any]] = []
            removed: list[str] = []
            last_commit = ""
            for number, (batch, batch_deletes) in enumerate(plan, 1):
                batch_files = [by_path[path] for path in batch]
                part = (
                    f"Part {number} of {count} of one change set split under the {_MAX_COMMIT_CHANGES} changes and "
                    f"{_MAX_COMMIT_CONTENT_BYTES} bytes one signed commit may carry."
                )
                result = await _create_github_commit(
                    repo=repo,
                    message=f"{message.rstrip()}\n\n{part}",
                    files=batch_files,
                    delete_paths=batch_deletes,
                    branch=scratch,
                )
                text = result.content if isinstance(result, ToolResult) else str(result)
                match = _COMMITTED_RE.search(text)
                if match:
                    last_commit = match.group(1)
                    signing = next((line for line in text.splitlines() if line.startswith(_SIGNING_PREFIXES)), "")
                    landed.append(
                        f"  {number}. {last_commit}: {_batch_line(batch, batch_deletes)}"
                        + (f" ({signing})" if signing else "")
                    )
                elif isinstance(result, ToolResult) and result.no_op:
                    landed.append(f"  {number}. no change (the branch already has it): {_batch_line(batch, batch_deletes)}")
                else:
                    return (
                        f"Commit {number} of {count} failed, so branch '{target}' of {repo} was not moved and is "
                        f"unchanged; the change set is still a change of the checkout:\n{text}"
                    )
                written.extend(batch_files)
                removed.extend(batch_deletes)
            if not last_commit:
                return "\n".join([f"Branch '{target}' of {repo} already has every change:", *landed, checkout_note])
            async with egress_client(timeout=60) as client:
                resp, err = await _common.safe_request(
                    client, "get", f"{repo_url}/git/ref/heads/{_enc(target)}", _PROVIDER,
                    f"re-read branch {target} of {repo}", headers=headers,
                )
                current_object = _json_object(resp).get("object") if not err else None
                current = _coerce_str(current_object.get("sha")) if isinstance(current_object, dict) else ""
                if current != tip:
                    return _moved_tip_refusal(target, repo, current)
                _, err = await _common.safe_request(
                    client, "patch", f"{repo_url}/git/refs/heads/{_enc(target)}", _PROVIDER,
                    f"move branch {target} of {repo} to the last split commit", headers=_json_headers(headers),
                    json={"sha": last_commit, "force": False},
                )
            if err:
                # The signed commits stay reachable from the scratch branch: a pull request can take them.
                keep_scratch = True
                return (
                    f"{err}\nThe change set was committed as {count} signed commits on scratch branch '{scratch}' "
                    f"(last {last_commit}), but branch '{target}' was not moved to it and is unchanged. The scratch "
                    f"branch is deleted by the next split commit on {repo} once it is a day old."
                )
        finally:
            if not keep_scratch:
                await drop_scratch()
        await _forget_cached_404s(headers, owner, name, target, list(by_path))
        lines = [
            f"Committed the change set to branch '{target}' of {repo} in {count} consecutive commits, each under "
            f"the {_MAX_COMMIT_CHANGES} changes and {_MAX_COMMIT_CONTENT_BYTES} bytes one signed commit may carry; "
            f"the branch moved once, to the last ({last_commit}), so it never pointed at a partial change set:",
            *landed,
            checkout_note,
        ]
        landed_paths = {str(item["path"]) for item in written}
        kept = {path: data for path, data in (rewrites or {}).items() if path in landed_paths}
        moved = await _advance_checkout(
            checkout_record, "" if keep_record_commit else last_commit, target, written, removed, kept
        )
        if moved:
            lines.append(moved)
        if workspace_written and checkout_record is None:
            moved = await _advance_checkout_files(
                repo, target, tip, last_commit, workspace_written, sorted(landed_paths) + list(removed)
            )
            if moved:
                lines.append(moved)
        named = await name_pushed_pull_request(repo, owner, name, target, last_commit)
        if named:
            lines.append(named)
        return "\n".join(lines)

    async def delete_github_file(repo: str, path: str, message: str, branch: str = "") -> str | ToolResult:
        """Delete a file from a repository branch: one commit GitHub builds and signs (#12051).

        Through the ``create_github_commit`` path, under the branch's write
        lock, like ``create_or_update_github_file``.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        clean_path = _coerce_str(path).strip().strip("/")
        async with branch_write_lock(repo, branch):
            committed = await _create_github_commit(
                repo=repo,
                message=_normalize_commit_message(message, clean_path, "delete"),
                delete_paths=[clean_path],
                branch=branch,
            )
        return _per_file_answer(committed, owner, name, clean_path, "delete")

    executor.register("get_github_file", get_github_file)
    executor.register("download_github_file", download_github_file)
    executor.register("download_github_repository_archive", download_github_repository_archive)
    executor.register("get_github_repository_tree", get_github_repository_tree)
    executor.register("create_or_update_github_file", create_or_update_github_file)
    executor.register("create_github_commit", create_github_commit)
    executor.register("delete_github_file", delete_github_file)

    return [
        ToolDefinition(
            name="get_github_file",
            description=(
                "Read a file's content from a GitHub repository by path and branch, a window at a time: max_chars "
                "characters from offset (the result says how many remain and the next offset), with the file's web "
                "and raw download links. For a whole file, or one you will process in code, pass to_workspace_path "
                "instead of reading it into the context; for a range of a large file, offset and max_chars."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "branch": {"type": "string", "description": "Branch name (default: the repository's default branch)"},
                    "offset": {"type": "integer", "description": "Character offset to start from (default 0)."},
                    "max_chars": {
                        "type": "integer",
                        "description": f"Characters to return (default {_DEFAULT_FILE_CHARS:,}, at most {_MAX_FILE_CHARS:,}).",
                    },
                },
                "required": ["repo", "path"],
            },
            source=_SOURCE,
            priority=0,
            workspace_ingest=True,
        ),
        ToolDefinition(
            name="download_github_file",
            description=(
                "Download a repository file (any type - an image, a PDF, a binary, a file "
                "too large to read inline - up to 100 MB) into the conversation: a download "
                "card, its links and a file_id to forward to any tool that takes a file. "
                "Its readable content comes with the result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "branch": {"type": "string", "description": "Branch, tag or commit SHA (default: the repository's default branch)"},
                },
                "required": ["repo", "path"],
            },
            source=_SOURCE,
            priority=1,
            workspace_ingest=True,
            repository_file=True,
        ),
        ToolDefinition(
            name="download_github_repository_archive",
            description=(
                "Download a repository's tree at a branch, tag or commit SHA as one tar.gz archive (no git "
                "history), up to 256 MB, into the conversation. To work on the files in code_interpreter, "
                "materialize_repository checks them out into its workspace instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "ref": {"type": "string", "description": "Branch, tag or commit SHA (default: the repository's default branch)"},
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Over 256 MB: keep only these paths (media outside them left out, named)",
                    },
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=2,
            workspace_ingest=True,
            repository_archive=True,
        ),
        ToolDefinition(
            name="get_github_repository_tree",
            description=(
                "List a repository's file tree recursively for a branch, tag, or commit SHA. "
                "Returns file/directory paths so you can discover the project layout in one call before "
                "reading specific files with get_github_file. Use the 'path' prefix to scope to a sub-directory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA (default: the repo's default branch)", "default": ""},
                    "path": {"type": "string", "description": "Only show entries under this directory prefix (optional)", "default": ""},
                    "count": {"type": "integer", "description": "Max entries to show (max 500)", "default": 200},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_or_update_github_file",
            description=(
                "Create or update a file in a GitHub repository. Provide the full file content as plain text — "
                "or file_id, any file of this conversation (an image, a PDF, a binary) committed byte-for-byte. "
                "Each call creates one commit GitHub signs on the target branch; calls on one branch run one "
                "after the other. For several files use ONE create_github_commit, not parallel calls. Use after "
                "create_github_branch to push changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "path": {"type": "string", "description": "File path within the repository (e.g. 'docs/guide.md')"},
                    "content": {"type": "string", "description": "Full file content as plain text (instead of file_id)"},
                    "file_id": {"type": "string", "description": "A file of this conversation to commit as-is (instead of content)"},
                    "message": {"type": "string", "description": "Git commit message describing the change"},
                    "branch": {
                        "type": "string",
                        "description": "Target branch to commit to (default: the repository's default branch)",
                        "default": "",
                    },
                },
                "required": ["repo", "path", "message"],
            },
            source=_SOURCE,
            priority=1,
            workspace_files=("file_id",),
        ),
        ToolDefinition(
            name="create_github_commit",
            description=(
                "Commit several file changes to a GitHub branch as ONE commit: write files (full plain-text "
                "content, or file_id for any file of this conversation - an image, a PDF, a binary - committed "
                "byte-for-byte; or from_checkout for everything the sandbox changed in a materialize_repository "
                "checkout; or 'edits', exact find/replace applied server-side to the file as it is at the branch "
                "tip, the contract of sandbox_edit_file: each find must exist and be unique unless replace_all, "
                "nothing committed unless every edit applies, the diff returned - to change a few lines without "
                "resending the file or using the sandbox) and delete files together. Prefer it to repeated "
                "create_or_update_github_file / "
                "delete_github_file calls when a change spans several files: the branch moves once, so it is never "
                "left half-updated and CI runs once. An existing file keeps its mode unless 'executable' is given. "
                "GitHub signs the commit, an edited script keeping its bit included, unless a file mode changes "
                "(a script created, renamed or no longer executable): that commit is unsigned, and the result says so. "
                f"Refused with the branch unchanged: more than {_MAX_COMMIT_CHANGES} changes or "
                f"{_MAX_COMMIT_CONTENT_BYTES // (1024 * 1024)} MB of content, writing over a directory, symbolic "
                "link or submodule, deleting a directory or submodule (a symbolic link itself can be deleted), and "
                "a branch that moved meanwhile. A from_checkout change set over "
                f"{_MAX_COMMIT_CHANGES} changes or {_MAX_COMMIT_CONTENT_BYTES // (1024 * 1024)} MB is not refused: it "
                "lands as consecutive signed commits under the cap (smallest files first), every SHA reported; a "
                "single file over the cap is refused. Use after create_github_branch, then open a pull request with create_github_pr."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "message": {"type": "string", "description": "Git commit message for the whole change"},
                    "files": {
                        "type": "array",
                        "description": "Files to create or overwrite. Give at least one of 'files' or 'delete_paths'.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "File path from the repository root (e.g. 'src/app.py')",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Full file content as plain text (instead of file_id)",
                                },
                                "file_id": {
                                    "type": "string",
                                    "description": "A file of this conversation to commit as-is (instead of content)",
                                },
                                "edits": {
                                    "type": "array",
                                    "description": (
                                        "Instead of content / file_id: ordered find/replace edits (at most "
                                        f"{editing.MAX_EDITS}) applied to the existing file at the branch tip; every "
                                        "find must exist and be unique (or set replace_all). A find absent or not "
                                        "unique refuses the whole commit, branch unchanged. The diff of each edited "
                                        "file is returned."
                                    ),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "find": {
                                                "type": "string",
                                                "description": "Exact text to replace (verbatim, whitespace included)",
                                            },
                                            "replace": {
                                                "type": "string",
                                                "description": "Replacement text; empty deletes the found text",
                                            },
                                            "replace_all": {
                                                "type": "boolean",
                                                "description": "Replace every occurrence (default false)",
                                            },
                                        },
                                        "required": ["find", "replace"],
                                    },
                                },
                                "executable": {
                                    "type": "boolean",
                                    "description": (
                                        "true for a script that must be executable, false to clear the bit; omit "
                                        "to keep an existing file's mode, across a case-only rename too (a new file "
                                        "is not executable). An executable bit set or cleared makes the commit "
                                        "unsigned"
                                    ),
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "delete_paths": {
                        "type": "array",
                        "description": "Paths of existing files to delete in the same commit",
                        "items": {"type": "string"},
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to commit to (default: the repository's default branch)",
                        "default": "",
                    },
                    "from_checkout": {
                        "type": "string",
                        "description": (
                            "A sandbox workspace directory written by materialize_repository: commit what the "
                            "sandbox changed under it (edited and new files written, removed files deleted), computed "
                            "from its .xtm-checkout record. Instead of 'files' and 'delete_paths'; nothing is "
                            "committed when the checkout is unchanged. Over the change-count or size cap of one "
                            "commit it is split into consecutive signed commits in this one call."
                        ),
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "With from_checkout: only the changes under these repository paths (files or directories).",
                    },
                },
                "required": ["repo", "message"],
            },
            source=_SOURCE,
            priority=1,
            workspace_files=("files[].file_id",),
            # A resumed turn's repeat of a commit that landed is answered with its sha.
            landed_probe=commit_probe(headers),
        ),
        ToolDefinition(
            name="delete_github_file",
            description=(
                "Delete a file from a GitHub repository branch: one commit GitHub signs. Calls on one branch run one "
                "after the other; for several files use ONE create_github_commit with delete_paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "path": {"type": "string", "description": "File path to delete (e.g. 'docs/obsolete.md')"},
                    "message": {"type": "string", "description": "Git commit message for the deletion"},
                    "branch": {
                        "type": "string",
                        "description": "Branch to delete from (default: the repository's default branch)",
                        "default": "",
                    },
                },
                "required": ["repo", "path", "message"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
