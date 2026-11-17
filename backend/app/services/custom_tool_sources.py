# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Sync custom tools from a GitHub or GitLab repository.

A :class:`~app.models.custom_tool_source.CustomToolSource` names a repository,
a ref (branch, tag or commit; the default branch when unset) and a
sub-directory. Every directory below it holding a ``tool.yaml`` (or
``tool.yml``) manifest is one custom tool::

    tools/whois-lookup/
      tool.yaml        # name, description, input_schema, timeout, tags, tests
      main.py          # the tool's code (``entry``)
      test_main.py     # optional test code (``test_file``)

The sync reads the commit the ref points at, then for each tool:

- **unchanged** (same files as the active version): nothing happens;
- **new or changed**: its declared tests run (``services/custom_tool_tests.py``)
  and the new version is **activated only when they pass** - a tool without
  tests is activated, and the report says it is untested. A rejected version
  leaves the active one in place (a new tool is not created);
- **gone** from the repository: the tool is disabled, never deleted - agents
  are bound to it;
- **detached** (``detached_paths``): left alone.

A synced tool's content is read-only everywhere
(:func:`app.api.v1.helpers.source_managed_custom_tool_error`): it changes in the
repository. Credentials are the bound integration's, never a new secret store;
without one, a public github.com / gitlab.com repository is read anonymously.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import EgressRoute, egress_client, egress_route
from app.core.utils import format_exception_message
from app.models.custom_tool import CustomTool
from app.models.custom_tool_source import CustomToolSource

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_WEB = "https://github.com"

#: The manifest file names, in order of preference within one directory.
MANIFEST_NAMES = ("tool.yaml", "tool.yml")
#: Entry files tried, in order, when a manifest names none.
DEFAULT_ENTRIES = ("main.py", "main.js", "index.js")
#: Test files tried, in order, when a manifest names none.
DEFAULT_TEST_FILES = ("test_main.py", "main.test.js", "test.js")
_LANGUAGE_BY_SUFFIX = {".py": "python", ".js": "javascript", ".cjs": "javascript", ".mjs": "javascript"}

#: Largest file read from the repository.
MAX_FILE_BYTES = 512 * 1024
#: Most tools one source may produce.
MAX_TOOLS_PER_SOURCE = 200
#: Most tree pages read from GitLab (100 entries each).
_MAX_GITLAB_TREE_PAGES = 100
_HTTP_TIMEOUT = 30
_MAX_BACKOFF_MINUTES = 360
_INTERVALS: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}


class SourceSyncError(Exception):
    """The source cannot be read at all (credential, repository, ref)."""


class SourceSyncBusyError(Exception):
    """Another sync of the same source is running."""


# ---------------------------------------------------------------------------
# Repository coordinates
# ---------------------------------------------------------------------------


def normalize_repository(provider: str, repository: str) -> str:
    """``owner/repo`` (GitHub) or ``group/.../project`` (GitLab) from a path or a URL.

    Raises ``ValueError`` for anything else. A URL's host is not kept: the
    host is github.com, gitlab.com, or the bound GitLab integration's.
    """
    text = (repository or "").strip()
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.path
    text = text.strip().strip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    # A web URL to a tree or a file: keep the project part.
    if "/-/" in text:
        text = text.split("/-/", 1)[0]
    segments = [s for s in text.split("/") if s]
    if provider == "github":
        if len(segments) >= 4 and segments[2] in ("tree", "blob"):
            segments = segments[:2]
        if len(segments) != 2:
            raise ValueError("A GitHub repository is written owner/repository.")
    elif len(segments) < 2:
        raise ValueError("A GitLab project is written group/project (subgroups included).")
    if any(s in (".", "..") or not all(c.isalnum() or c in "-_." for c in s) for s in segments):
        raise ValueError("The repository path holds an invalid segment.")
    return "/".join(segments)


def normalize_subpath(path: str | None) -> str:
    """A repository sub-directory without leading/trailing slashes, or raise ``ValueError``."""
    text = (path or "").strip().strip("/")
    if not text:
        return ""
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("The sub-directory must be a plain relative path.")
    return text


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RepoAccess:
    """How to reach a source's repository. ``token`` is decrypted in memory only."""

    provider: str
    api_base: str
    web_base: str
    token: str | None = None
    #: An OAuth token (``Authorization: Bearer``) rather than an access token
    #: (GitLab's ``PRIVATE-TOKEN``).
    oauth: bool = False
    route: EgressRoute | None = None
    integration_name: str | None = None


async def resolve_access(db: AsyncSession, source: CustomToolSource) -> RepoAccess:
    """The credential and endpoints the sync of *source* uses.

    Like skill sources, there is no auto-discovery: only the integration an
    operator bound is used, never one substituted at sync time.
    """
    from app.integrations.providers.gitlab import GITLAB_DEFAULT_URL, gitlab_api_base

    if source.provider == "github":
        access = RepoAccess(provider="github", api_base=GITHUB_API, web_base=GITHUB_WEB)
    elif source.provider == "gitlab":
        access = RepoAccess(provider="gitlab", api_base=f"{GITLAB_DEFAULT_URL}/api/v4", web_base=GITLAB_DEFAULT_URL)
    else:
        raise SourceSyncError(f"Unknown repository provider '{source.provider}'.")
    if source.integration_id is None:
        return access

    from app.core.security import decrypt_value
    from app.models.integration import Integration
    from app.services.integration_user_credentials import is_per_user
    from app.services.relays import integration_egress_route
    from app.services.vault import SecretUnavailableError, reveal_field, reveal_json_field

    integration = (
        await db.execute(select(Integration).where(Integration.id == source.integration_id))
    ).scalar_one_or_none()
    if integration is None:
        raise SourceSyncError(
            "The integration bound to this source no longer exists. Bind another one, or none for a public repository."
        )
    if integration.provider != source.provider:
        raise SourceSyncError(
            f"Integration '{integration.name}' is a {integration.provider} integration, "
            f"not a {source.provider} one."
        )
    if integration.status != "connected":
        raise SourceSyncError(
            f"Integration '{integration.name}' is not connected (status: {integration.status})."
        )
    if is_per_user(integration):
        raise SourceSyncError(
            f"Integration '{integration.name}' acts with each person's own account, so it cannot "
            "read a source that syncs unattended. Bind an integration that shares its account."
        )
    try:
        if integration.access_token_enc:
            access.token = decrypt_value(integration.access_token_enc)
            access.oauth = True
        else:
            access.token = await reveal_field(integration, "api_key_enc")
        if source.provider == "gitlab" and integration.extra_config_enc:
            extra = await reveal_json_field(integration, "extra_config_enc")
            base_url = (extra.get("base_url") or "").strip() if isinstance(extra, dict) else ""
            api_base = gitlab_api_base(base_url)
            if api_base is None:
                raise SourceSyncError(f"Integration '{integration.name}' has an invalid GitLab URL.")
            access.api_base = api_base
            access.web_base = api_base[: -len("/api/v4")]
    except SecretUnavailableError as exc:
        raise SourceSyncError(str(exc)) from None
    if not access.token:
        raise SourceSyncError(f"Integration '{integration.name}' holds no token.")
    access.integration_name = integration.name
    access.route = await integration_egress_route(db, integration)
    return access


# ---------------------------------------------------------------------------
# Repository readers
# ---------------------------------------------------------------------------


def _http_error(access: RepoAccess, status: int, headers: Any, what: str) -> SourceSyncError:
    from app.services.skill_source import classify_github_failure

    host = "GitHub" if access.provider == "github" else "GitLab"
    kind = classify_github_failure(status, headers)
    if kind == "auth":
        who = f"the credential of '{access.integration_name}'" if access.integration_name else "an anonymous request"
        return SourceSyncError(f"{host} refused {who} (HTTP {status}) while reading {what}.")
    if kind == "rate_limit":
        return SourceSyncError(f"{host} is throttling requests (HTTP {status}). The next sync retries.")
    if kind == "not_found":
        hint = "" if access.integration_name else " A private repository needs a bound integration."
        return SourceSyncError(f"{host} returned HTTP 404 for {what}.{hint}")
    return SourceSyncError(f"{host} answered HTTP {status} while reading {what}.")


class RepoReader:
    """Reads one repository at one commit."""

    def __init__(self, access: RepoAccess, repository: str, client: Any) -> None:
        self.access = access
        self.repository = repository
        self.client = client

    async def _get(self, url: str, what: str, *, params: dict | None = None, raw: bool = False) -> Any:
        resp = await self.client.get(url, params=params, headers=self._headers(raw=raw))
        if resp.status_code >= 400:
            raise _http_error(self.access, resp.status_code, resp.headers, what)
        return resp

    def _headers(self, *, raw: bool) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def head_commit(self, ref: str | None) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    async def list_files(self, sha: str, subpath: str) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def read_file(self, sha: str, path: str) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def web_url(self, sha: str, path: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


def _check_size(content: bytes, path: str) -> bytes:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_FILE_BYTES // 1024} KB.")
    return content


class GitHubReader(RepoReader):
    def _headers(self, *, raw: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.access.token:
            headers["Authorization"] = f"Bearer {self.access.token}"
        return headers

    @property
    def _repo_api(self) -> str:
        owner, name = self.repository.split("/", 1)
        return f"{self.access.api_base}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    async def head_commit(self, ref: str | None) -> str:
        if not ref:
            meta = (await self._get(self._repo_api, "the repository")).json()
            ref = meta.get("default_branch") or "main"
        resp = await self.client.get(
            f"{self._repo_api}/commits/{quote(ref, safe='')}", headers=self._headers(raw=False)
        )
        if resp.status_code == 422:  # GitHub's answer for a ref that does not exist
            raise SourceSyncError(f"The ref '{ref}' does not exist in {self.repository}.")
        if resp.status_code >= 400:
            raise _http_error(self.access, resp.status_code, resp.headers, f"the ref '{ref}'")
        data = resp.json()
        sha = data.get("sha") if isinstance(data, dict) else None
        if not sha:
            raise SourceSyncError(f"GitHub returned no commit for the ref '{ref}'.")
        return str(sha)

    async def _tree(self, tree_sha: str, what: str, *, recursive: bool) -> dict[str, Any]:
        params = {"recursive": "1"} if recursive else None
        data = (await self._get(f"{self._repo_api}/git/trees/{tree_sha}", what, params=params)).json()
        return data if isinstance(data, dict) else {}

    async def _subtree_sha(self, sha: str, subpath: str) -> str | None:
        """The tree of *subpath* at commit *sha*, one directory level at a
        time, or ``None`` when it does not exist."""
        current = sha
        walked: list[str] = []
        for segment in subpath.split("/"):
            data = await self._tree(current, f"the directory '{'/'.join(walked) or '/'}'", recursive=False)
            entry = next(
                (i for i in data.get("tree", []) if i.get("path") == segment and i.get("type") == "tree"), None
            )
            if entry is None:
                if data.get("truncated"):
                    raise SourceSyncError(
                        f"The directory '{'/'.join(walked) or '/'}' is too large to list in one read."
                    )
                return None
            current = str(entry["sha"])
            walked.append(segment)
        return current

    async def list_files(self, sha: str, subpath: str) -> list[str]:
        # The recursive read starts at the sub-directory, so a large monorepo
        # syncs as long as the directory holding the tools fits in one read.
        tree_sha = await self._subtree_sha(sha, subpath) if subpath else sha
        if tree_sha is None:
            return []
        data = await self._tree(tree_sha, "the repository tree", recursive=True)
        if data.get("truncated"):
            raise SourceSyncError(
                f"The sub-directory '{subpath}' is too large to list in one read. Set a narrower one."
                if subpath
                else "The repository is too large to list in one read. Set a sub-directory holding the tools."
            )
        prefix = f"{subpath}/" if subpath else ""
        return [f"{prefix}{item['path']}" for item in data.get("tree", []) if item.get("type") == "blob"]

    async def read_file(self, sha: str, path: str) -> bytes:
        if self.access.token:
            url = f"{self._repo_api}/contents/{quote(path)}"
            resp = await self._get(url, path, params={"ref": sha}, raw=True)
        else:
            # Anonymous reads go to the raw host: the API's anonymous budget
            # (60 requests an hour) would not cover a repository of tools.
            url = f"{GITHUB_RAW}/{self.repository}/{sha}/{quote(path)}"
            resp = await self.client.get(url)
            if resp.status_code >= 400:
                raise _http_error(self.access, resp.status_code, resp.headers, path)
        return _check_size(resp.content, path)

    def web_url(self, sha: str, path: str) -> str:
        return f"{self.access.web_base}/{self.repository}/tree/{sha}/{quote(path)}"


class GitLabReader(RepoReader):
    def _headers(self, *, raw: bool) -> dict[str, str]:
        headers = {"Accept": "*/*" if raw else "application/json"}
        if self.access.token:
            if self.access.oauth:
                headers["Authorization"] = f"Bearer {self.access.token}"
            else:
                headers["PRIVATE-TOKEN"] = self.access.token
        return headers

    @property
    def _project_api(self) -> str:
        return f"{self.access.api_base}/projects/{quote(self.repository, safe='')}"

    async def head_commit(self, ref: str | None) -> str:
        if not ref:
            meta = (await self._get(self._project_api, "the project")).json()
            ref = meta.get("default_branch") or "main"
        data = (
            await self._get(
                f"{self._project_api}/repository/commits/{quote(ref, safe='')}", f"the ref '{ref}'"
            )
        ).json()
        sha = data.get("id") if isinstance(data, dict) else None
        if not sha:
            raise SourceSyncError(f"GitLab returned no commit for the ref '{ref}'.")
        return str(sha)

    async def list_files(self, sha: str, subpath: str) -> list[str]:
        files: list[str] = []
        page = "1"
        for _ in range(_MAX_GITLAB_TREE_PAGES):
            params = {"ref": sha, "recursive": "true", "per_page": "100", "page": page}
            if subpath:
                params["path"] = subpath
            resp = await self.client.get(
                f"{self._project_api}/repository/tree", params=params, headers=self._headers(raw=False)
            )
            if resp.status_code == 404 and subpath:  # GitLab's answer for a missing directory
                return []
            if resp.status_code >= 400:
                raise _http_error(self.access, resp.status_code, resp.headers, "the repository tree")
            files.extend(item["path"] for item in resp.json() if item.get("type") == "blob")
            page = resp.headers.get("x-next-page") or ""
            if not page:
                return files
        raise SourceSyncError("The repository is too large to list. Set a sub-directory holding the tools.")

    async def read_file(self, sha: str, path: str) -> bytes:
        url = f"{self._project_api}/repository/files/{quote(path, safe='')}/raw"
        resp = await self._get(url, path, params={"ref": sha}, raw=True)
        return _check_size(resp.content, path)

    def web_url(self, sha: str, path: str) -> str:
        return f"{self.access.web_base}/{self.repository}/-/tree/{sha}/{quote(path)}"


def make_reader(access: RepoAccess, repository: str, client: Any) -> RepoReader:
    return (GitHubReader if access.provider == "github" else GitLabReader)(access, repository, client)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


class ToolManifest(BaseModel):
    """``tool.yaml``: what a repository directory declares about its tool."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    language: str | None = None
    entry: str | None = None
    input_schema: dict[str, Any] | None = None
    timeout: int = Field(default=300, ge=1, le=3600)
    tags: list[str] = Field(default_factory=list)
    icon_url: str | None = Field(default=None, max_length=2000)
    hide_from_chat: bool = False
    tests: list[dict[str, Any]] | None = None
    test_file: str | None = None

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value in ("js", "node"):
            value = "javascript"
        if value not in ("javascript", "python"):
            raise ValueError("must be 'python' or 'javascript'")
        return value

    @field_validator("input_schema")
    @classmethod
    def _object_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and value.get("type", "object") != "object":
            raise ValueError("must be a JSON Schema of type object")
        return value


def _relative(base: str, name: str, field_name: str) -> str:
    """*name* inside the tool directory *base*, or raise ``ValueError``."""
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError(f"{field_name} must be a path relative to the tool's directory.")
    joined = posixpath.normpath(posixpath.join(base, name)) if base else posixpath.normpath(name)
    if joined.startswith("..") or (base and not joined.startswith(f"{base}/")):
        raise ValueError(f"{field_name} must stay inside the tool's directory.")
    return joined


@dataclass(slots=True)
class ToolBundle:
    """One tool read from the repository, ready to compare and apply."""

    path: str
    manifest: ToolManifest
    language: str
    code: str
    test_cases: list[dict[str, Any]] | None
    test_code: str | None
    content_hash: str
    files: list[str] = field(default_factory=list)


def _decode(content: bytes, path: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text.") from None


async def read_tool(reader: RepoReader, sha: str, directory: str, manifest_path: str, files: set[str]) -> ToolBundle:
    """Read and validate the tool of *directory*, or raise ``ValueError``."""
    from app.services.custom_tool_tests import validate_test_cases, validate_test_code

    raw_manifest = await reader.read_file(sha, manifest_path)
    try:
        data = yaml.safe_load(_decode(raw_manifest, manifest_path))
    except yaml.YAMLError as exc:
        raise ValueError(f"{manifest_path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path} must be a mapping of fields.")
    try:
        manifest = ToolManifest.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ValueError(f"{manifest_path}: {where}: {first.get('msg')}") from None

    if manifest.entry:
        entry = _relative(directory, manifest.entry, "entry")
        if entry not in files:
            raise ValueError(f"The entry file {entry} does not exist.")
    else:
        candidates = [_relative(directory, name, "entry") for name in DEFAULT_ENTRIES]
        entry = next((c for c in candidates if c in files), "")
        if not entry:
            raise ValueError(
                f"No entry file in {directory or 'the root'}: add main.py, main.js or index.js, or set 'entry'."
            )
    language = manifest.language or _LANGUAGE_BY_SUFFIX.get(posixpath.splitext(entry)[1], "")
    if not language:
        raise ValueError(f"Cannot tell the language of {entry}: set 'language'.")

    test_path: str | None = None
    if manifest.test_file:
        test_path = _relative(directory, manifest.test_file, "test_file")
        if test_path not in files:
            raise ValueError(f"The test file {test_path} does not exist.")
    else:
        suffix = ".py" if language == "python" else ".js"
        for name in DEFAULT_TEST_FILES:
            candidate = _relative(directory, name, "test_file")
            if candidate.endswith(suffix) and candidate in files:
                test_path = candidate
                break

    code_bytes = await reader.read_file(sha, entry)
    test_bytes = await reader.read_file(sha, test_path) if test_path else b""
    test_cases = validate_test_cases(manifest.tests)
    test_code = validate_test_code(_decode(test_bytes, test_path)) if test_path else None

    digest = hashlib.sha256()
    for name, content in ((manifest_path, raw_manifest), (entry, code_bytes), (test_path or "", test_bytes)):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return ToolBundle(
        path=directory,
        manifest=manifest,
        language=language,
        code=_decode(code_bytes, entry),
        test_cases=test_cases,
        test_code=test_code,
        content_hash=digest.hexdigest(),
        files=[p for p in (manifest_path, entry, test_path) if p],
    )


def find_manifests(files: list[str]) -> dict[str, str]:
    """``{tool directory: manifest path}`` for every manifest in *files*."""
    found: dict[str, str] = {}
    for path in sorted(files):
        directory, name = posixpath.split(path)
        if name in MANIFEST_NAMES:
            current = found.get(directory)
            if current is None or MANIFEST_NAMES.index(name) < MANIFEST_NAMES.index(posixpath.basename(current)):
                found[directory] = path
    return found


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SyncReport:
    commit_sha: str | None = None
    #: The repository host's web root, for links to the tools' directories.
    web_base: str | None = None
    created: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    disabled: list[dict[str, Any]] = field(default_factory=list)
    #: Tools the sync had disabled whose directory came back: enabled again
    #: (each is also in ``updated`` or ``unchanged``).
    restored: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    skipped_detached: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "partial" if self.rejected or self.errors else "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "web_base": self.web_base,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "disabled": self.disabled,
            "restored": self.restored,
            "errors": self.errors,
            "skipped_detached": self.skipped_detached,
        }


def _test_digest(report: dict[str, Any]) -> dict[str, Any]:
    """The part of a test report a sync report keeps."""
    return {
        "status": report["status"],
        "summary": report["summary"],
        "failures": [
            {k: r.get(k) for k in ("kind", "name", "status", "message") if r.get(k) is not None}
            for r in report["results"]
            if r["status"] in ("failed", "error")
        ][:20],
    }


async def _owner_variables(db: AsyncSession, source: CustomToolSource) -> dict[str, Any]:
    """The platform variables the source's tests run with: its owner's."""
    if source.user_id is None:
        return {}
    from app.api.v1.helpers import get_user_group_ids
    from app.services.variable_resolver import load_variables

    return await load_variables(db, str(source.user_id), await get_user_group_ids(db, source.user_id))


async def _run_bundle_tests(db: AsyncSession, source: CustomToolSource, bundle: ToolBundle, variables: dict) -> dict[str, Any]:
    from app.services.custom_tool_tests import run_tool_tests
    from app.services.variable_resolver import substitute_variables

    return await run_tool_tests(
        code=substitute_variables(bundle.code, variables),
        language=bundle.language,
        timeout=bundle.manifest.timeout,
        test_cases=bundle.test_cases,
        test_code=substitute_variables(bundle.test_code, variables) if bundle.test_code else None,
        user_id=source.user_id,
        db=db,
    )


def _apply(tool: CustomTool, source: CustomToolSource, bundle: ToolBundle, sha: str) -> None:
    manifest = bundle.manifest
    tool.name = manifest.name
    tool.description = manifest.description
    tool.icon_url = manifest.icon_url
    tool.input_schema = manifest.input_schema
    tool.js_function = bundle.code
    tool.language = bundle.language
    tool.timeout = manifest.timeout
    tool.tags = list(manifest.tags)
    tool.test_cases = bundle.test_cases
    tool.test_code = bundle.test_code
    tool.source_id = source.id
    tool.source_path = bundle.path
    tool.source_commit_sha = sha
    tool.source_content_hash = bundle.content_hash
    tool.user_id = source.user_id
    tool.is_company_managed = source.is_company_managed


def _restore(tool: CustomTool, report: SyncReport, entry: dict[str, Any]) -> None:
    """Enable *tool* again if the sync disabled it when its directory left the
    repository; a tool disabled by hand stays disabled."""
    if tool.source_removed_at is None:
        return
    tool.source_removed_at = None
    tool.enabled = True
    report.restored.append({**entry, "tool_id": str(tool.id)})


async def sync_source(db: AsyncSession, source: CustomToolSource) -> SyncReport:
    """Sync *source* now and return what it did. Commits nothing.

    Raises :class:`SourceSyncError` when the repository cannot be read at all;
    a tool that cannot be read or fails its tests is reported, the others go on.
    """
    from app.services.custom_tool_tests import ACTIVATING_STATUSES, store_test_report

    report = SyncReport()
    access = await resolve_access(db, source)
    report.web_base = access.web_base
    async with egress_client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        with egress_route(access.route):
            reader = make_reader(access, source.repository, client)
            sha = await reader.head_commit(source.ref)
            report.commit_sha = sha
            files = await reader.list_files(sha, source.path)
            if source.path and not files:
                # A mistyped sub-directory must not read as "every tool was
                # removed" and disable them all.
                raise SourceSyncError(f"The sub-directory '{source.path}' does not exist at this commit.")
            manifests = find_manifests(files)
            if len(manifests) > MAX_TOOLS_PER_SOURCE:
                raise SourceSyncError(
                    f"The repository declares {len(manifests)} tools; a source syncs at most {MAX_TOOLS_PER_SOURCE}."
                )
            file_set = set(files)
            detached = set(source.detached_paths or [])
            bundles: dict[str, ToolBundle] = {}
            for directory, manifest_path in manifests.items():
                if directory in detached:
                    report.skipped_detached.append(directory)
                    continue
                try:
                    bundles[directory] = await read_tool(reader, sha, directory, manifest_path, file_set)
                except (ValueError, SourceSyncError) as exc:
                    report.errors.append({"path": directory, "error": str(exc)})

    existing = {
        tool.source_path: tool
        for tool in (
            await db.execute(select(CustomTool).where(CustomTool.source_id == source.id))
        ).scalars()
        if tool.source_path
    }
    variables: dict[str, Any] | None = None
    for directory, bundle in bundles.items():
        tool = existing.get(directory)
        entry = {"path": directory, "name": bundle.manifest.name}
        if tool is not None and tool.source_content_hash == bundle.content_hash:
            tool.source_commit_sha = sha
            report.unchanged.append({**entry, "tool_id": str(tool.id)})
            _restore(tool, report, entry)
            continue
        if variables is None:
            variables = await _owner_variables(db, source)
        tests = await _run_bundle_tests(db, source, bundle, variables)
        if tests["status"] not in ACTIVATING_STATUSES:
            report.rejected.append({**entry, "tool_id": str(tool.id) if tool else None, "tests": _test_digest(tests)})
            continue
        created = tool is None
        if tool is None:
            tool = CustomTool(enabled=True, hide_from_chat=bundle.manifest.hide_from_chat)
            db.add(tool)
        _apply(tool, source, bundle, sha)
        store_test_report(tool, tests)
        await db.flush()
        (report.created if created else report.updated).append(
            {**entry, "tool_id": str(tool.id), "tests": tests["status"]}
        )
        _restore(tool, report, entry)

    errored = {e["path"] for e in report.errors}
    for directory, tool in existing.items():
        if directory in bundles or directory in errored or directory in detached:
            continue
        if tool.enabled:
            tool.enabled = False
            tool.source_removed_at = datetime.now(UTC)
            report.disabled.append({"path": directory, "name": tool.name, "tool_id": str(tool.id)})
    return report


async def run_sync(db: AsyncSession, source: CustomToolSource, *, actor: Any = None) -> SyncReport | None:
    """Sync *source*, record the outcome on it and in the audit log, and commit.

    Returns the report, or ``None`` when the source could not be read (its
    ``last_sync_error`` says why). *actor* is the user who asked; ``None``
    for the scheduler.
    """
    from app.services.audit import log_audit, log_system_audit

    now = datetime.now(UTC)
    label = f"{source.id} ({source.provider} {source.repository})"
    # One sync of a source at a time: two would both see a new tool as missing
    # and each create it. The row lock lasts until the commit below.
    locked = (
        await db.execute(
            select(CustomToolSource.id)
            .where(CustomToolSource.id == source.id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise SourceSyncBusyError(f"A sync of the source '{source.name}' is already running.")
    try:
        report = await sync_source(db, source)
    except Exception as exc:
        # Nothing a failed sync wrote is kept; the source row is read again.
        await db.rollback()
        await db.refresh(source)
        message = str(exc) if isinstance(exc, SourceSyncError) else "The sync failed unexpectedly. Check the server logs."
        logger.warning(
            "Custom tool source %s failed to sync: %s", label, format_exception_message(exc)
        )
        source.last_sync_status = "error"
        source.last_sync_error = message[:2000]
        source.last_synced_at = now
        source.sync_failure_count = (source.sync_failure_count or 0) + 1
        backoff = min(2 ** source.sync_failure_count * 5, _MAX_BACKOFF_MINUTES)
        source.sync_next_retry_at = now + timedelta(minutes=backoff)
        await db.commit()
        return None

    source.last_synced_at = now
    source.last_commit_sha = report.commit_sha
    source.last_sync_status = report.status
    source.last_sync_error = None
    source.last_sync_report = report.as_dict()
    source.sync_failure_count = 0
    source.sync_next_retry_at = None
    if report.created or report.updated or report.rejected or report.disabled or report.restored:
        details = {
            "commit_sha": report.commit_sha,
            "created": [e["path"] for e in report.created],
            "updated": [e["path"] for e in report.updated],
            "rejected": [e["path"] for e in report.rejected],
            "disabled": [e["path"] for e in report.disabled],
            "restored": [e["path"] for e in report.restored],
        }
        summary = (
            f"Synced custom tool source '{source.name}' at {str(report.commit_sha)[:12]}: "
            f"{len(report.created)} created, {len(report.updated)} updated, "
            f"{len(report.rejected)} rejected by their tests, {len(report.disabled)} disabled, "
            f"{len(report.restored)} enabled again"
        )
        if actor is not None:
            await log_audit(
                db, user=actor, action="sync", entity_type="custom_tool_source",
                entity_id=source.id, entity_name=source.name, summary=summary, details=details,
            )
        elif source.user_id is not None:
            await log_system_audit(
                db, actor="custom_tool_source_sync", anchor_user_id=source.user_id, action="sync",
                entity_type="custom_tool_source", entity_id=source.id, entity_name=source.name,
                summary=summary, details=details,
            )
    await db.commit()
    return report


async def process_scheduled_syncs(db: AsyncSession) -> int:
    """Run every enabled source whose interval is due. Returns how many ran."""
    from app.core.security_options import security_option
    from app.services import agentic_gate

    if security_option("disable_custom_tools") or not await agentic_gate.is_licensed():
        return 0
    now = datetime.now(UTC)
    sources = (
        (await db.execute(select(CustomToolSource).where(CustomToolSource.enabled.is_(True)))).scalars().all()
    )
    ran = 0
    for source in sources:
        delta = _INTERVALS.get(source.sync_interval)
        if delta is None:  # "manual"
            continue
        retry_at = _aware(source.sync_next_retry_at)
        if retry_at and retry_at > now:
            continue
        last = _aware(source.last_synced_at)
        if last and last + delta > now and not retry_at:
            continue
        try:
            await run_sync(db, source)
        except SourceSyncBusyError:  # a manual sync of it is running
            continue
        ran += 1
    return ran


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def delete_source_tools(db: AsyncSession, source_id: uuid.UUID) -> int:
    """Delete the tools *source_id* produced, unbinding them from agents."""
    from sqlalchemy import Text, cast

    from app.models.agent import Agent

    tools = (await db.execute(select(CustomTool).where(CustomTool.source_id == source_id))).scalars().all()
    for tool in tools:
        tid = str(tool.id)
        agents = (
            await db.execute(select(Agent).where(cast(Agent.custom_tool_ids, Text).contains(tid)))
        ).scalars().all()
        for agent in agents:
            if agent.custom_tool_ids and tid in agent.custom_tool_ids:
                agent.custom_tool_ids = [v for v in agent.custom_tool_ids if v != tid]
        await db.delete(tool)
    return len(tools)


async def detach_source_tools(db: AsyncSession, source_id: uuid.UUID) -> int:
    """Turn the tools *source_id* produced into ordinary, editable tools."""
    tools = (await db.execute(select(CustomTool).where(CustomTool.source_id == source_id))).scalars().all()
    for tool in tools:
        detach_tool(tool)
    return len(tools)


def detach_tool(tool: CustomTool) -> None:
    """Make *tool* an ordinary tool; its source no longer touches it."""
    tool.source_id = None
    tool.source_path = None
    tool.source_commit_sha = None
    tool.source_content_hash = None
    tool.source_removed_at = None


def source_web_url(source: CustomToolSource) -> str:
    """The repository's web page (github.com, gitlab.com or the bound instance)."""
    base = (source.last_sync_report or {}).get("web_base") or (
        GITHUB_WEB if source.provider == "github" else "https://gitlab.com"
    )
    return f"{base}/{source.repository}"


def tool_web_url(source: CustomToolSource, tool: CustomTool) -> str | None:
    """The web page of *tool*'s directory at its active commit."""
    if not tool.source_commit_sha or tool.source_path is None:
        return None
    marker = "tree" if source.provider == "github" else "-/tree"
    path = f"/{quote(tool.source_path)}" if tool.source_path else ""
    return f"{source_web_url(source)}/{marker}/{tool.source_commit_sha}{path}"
