# SPDX-License-Identifier: Apache-2.0

"""GitHub and GitLab repositories as scheduled asset sources, with their SBOM.

Two sources of the ``assets`` section, read with the administrator's connected
GitHub or GitLab integration, read-only:

- ``github``: the repositories of the chosen organizations (or every repository
  the account reaches), and each one's dependency graph SBOM (SPDX,
  ``GET /repos/{owner}/{repo}/dependency-graph/sbom``) parsed by
  :mod:`app.services.sbom_parser`, the one SBOM parser of XTM One;
- ``gitlab``: the projects of the chosen groups, subgroups included (or every
  project the token is a member of), and each one's dependency list
  (``GET /projects/:id/dependencies``, GitLab Ultimate; another tier answers
  "not available on this GitLab tier" and the project still syncs).

A repository becomes one asset row, category ``source_code`` and subcategory
``code_repository`` (``dev-docs/asset-model.md`` section 1), keyed on its host
and its numeric id (identity key ``cloud_resource_id``, so a rename keeps the
asset); its dependencies become a software document anchored on that row
(``organization_asset_software_docs``), exactly as the endpoint sources do, with
the PURL and CPE the source gives. Nothing is written to GitHub or GitLab.

The mapping is pure and tested on fixtures; the readers page (``Link`` and
``X-Next-Page``), wait out a 429 with its ``Retry-After`` (bounded) and answer
a refused permission with the scope to grant.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

GITHUB = "github"
GITLAB = "gitlab"
SOURCES: tuple[str, ...] = (GITHUB, GITLAB)
LABELS: dict[str, str] = {GITHUB: "GitHub", GITLAB: "GitLab"}

GITHUB_API = "https://api.github.com"
GITHUB_HOST = "github.com"

#: The read permissions each source needs, named in the error when the API refuses.
REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    GITHUB: ("repo (or Contents: read and Metadata: read on a fine-grained token)", "read:org"),
    GITLAB: ("read_api",),
}
#: The filters each source supports, in the order the Sources panel shows them.
FILTERS: dict[str, tuple[str, ...]] = {
    GITHUB: ("organizations", "include_archived", "include_forks", "skip_dependencies"),
    GITLAB: ("groups", "include_archived", "include_forks", "skip_dependencies"),
}
#: Repositories read per run at most is the caller's ``limit``; this is the API page size.
_PAGE = 100
#: Concurrent SBOM reads.
_SBOM_CONCURRENCY = 4
#: Dependencies kept per repository at most (a larger list is reported cut).
MAX_DEPENDENCIES = 10_000
_RETRIES = 3
_MAX_WAIT_S = 30.0

#: GitLab package managers as PURL types (https://github.com/package-url/purl-spec).
_PURL_TYPES: dict[str, str] = {
    "npm": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "bower": "bower",
    "pip": "pypi",
    "pipenv": "pypi",
    "poetry": "pypi",
    "setuptools": "pypi",
    "uv": "pypi",
    "conda": "conda",
    "maven": "maven",
    "gradle": "maven",
    "sbt": "maven",
    "bundler": "gem",
    "composer": "composer",
    "go": "golang",
    "nuget": "nuget",
    "conan": "conan",
    "cargo": "cargo",
    "swift": "swift",
    "cocoapods": "cocoapods",
}

REASON_NOT_CONNECTED = "not_connected"
REASON_NO_TOKEN = "no_token"  # noqa: S105 - a reason code, not a secret
#: Why a repository's dependencies are not listed, as a code the Inventory words.
SBOM_NOT_ENABLED = "dependency_graph_disabled"
SBOM_TIER = "not_available_on_tier"


# ── Mapping (pure) ────────────────────────────────────────────────────


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _flag(filters: dict[str, Any], key: str) -> bool:
    value = filters.get(key)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _names(filters: dict[str, Any], key: str) -> list[str]:
    value = filters.get(key) or []
    if isinstance(value, str):
        value = [v for v in re.split(r"[\s,]+", value) if v]
    return list(dict.fromkeys(_text(v, 200).strip("/") for v in value if _text(v, 200).strip("/")))


def repository_key(host: str, repository_id: Any) -> str:
    """The identity key of a repository: its host and numeric id, stable across renames and transfers."""
    return f"{(host or '').strip().lower()}:repository:{_text(repository_id, 40)}"


def _tags(*, visibility: str, language: str, topics: list[Any], archived: bool, fork: bool) -> list[str]:
    tags = [f"visibility:{visibility}"] if visibility else []
    if language:
        tags.append(f"language:{language.lower()}")
    if archived:
        tags.append("archived")
    if fork:
        tags.append("fork")
    tags.extend(f"topic:{_text(t, 60).lower()}" for t in topics or [] if _text(t, 60))
    return tags[:50]


def github_repo_row(repo: dict[str, Any], *, host: str = GITHUB_HOST) -> dict[str, Any]:
    """A GitHub repository as an asset row (source ``github``, category ``source_code``)."""
    archived = bool(repo.get("archived"))
    visibility = _text(repo.get("visibility"), 20).lower() or ("private" if repo.get("private") else "public")
    raw_owner = repo.get("owner")
    owner: dict[str, Any] = raw_owner if isinstance(raw_owner, dict) else {}
    tags = _tags(
        visibility=visibility,
        language=_text(repo.get("language"), 40),
        topics=list(repo.get("topics") or []),
        archived=archived,
        fork=bool(repo.get("fork")),
    )
    login = _text(owner.get("login"), 100)
    if login:
        tags.insert(0, f"owner:{login.lower()}")
    branch = _text(repo.get("default_branch"), 100)
    if branch:
        tags.append(f"default-branch:{branch}")
    return {
        "name": _text(repo.get("full_name")) or _text(repo.get("name")) or _text(repo.get("id")),
        "category": "source_code",
        "subcategory": "code_repository",
        "internet_facing": "yes" if visibility == "public" else "no",
        "tags": tags[:50],
        "source": GITHUB,
        "external_id": _text(repo.get("id"), 40),
        "cloud_resource_id": repository_key(host, repo.get("id")),
        "url": _text(repo.get("html_url"), 500),
        "last_seen": _text(repo.get("pushed_at"), 40),
        "status": "inactive" if archived else "active",
    }


def gitlab_project_row(project: dict[str, Any], *, host: str) -> dict[str, Any]:
    """A GitLab project as an asset row (source ``gitlab``, category ``source_code``)."""
    archived = bool(project.get("archived"))
    visibility = _text(project.get("visibility"), 20).lower()
    raw_namespace = project.get("namespace")
    namespace: dict[str, Any] = raw_namespace if isinstance(raw_namespace, dict) else {}
    tags = _tags(
        visibility=visibility,
        language="",
        topics=list(project.get("topics") or project.get("tag_list") or []),
        archived=archived,
        fork=bool(project.get("forked_from_project")),
    )
    group = _text(namespace.get("full_path"), 200)
    if group:
        tags.insert(0, f"owner:{group.lower()}")
    branch = _text(project.get("default_branch"), 100)
    if branch:
        tags.append(f"default-branch:{branch}")
    return {
        "name": _text(project.get("path_with_namespace")) or _text(project.get("name")) or _text(project.get("id")),
        "category": "source_code",
        "subcategory": "code_repository",
        # GitLab "internal" is every signed-in user of the instance: not the Internet.
        "internet_facing": "yes" if visibility == "public" else "no",
        "tags": tags[:50],
        "source": GITLAB,
        "external_id": _text(project.get("id"), 40),
        "cloud_resource_id": repository_key(host, project.get("id")),
        "url": _text(project.get("web_url"), 500),
        "last_seen": _text(project.get("last_activity_at"), 40),
        "status": "inactive" if archived else "active",
    }


def keep_repository(row: dict[str, Any], raw: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Whether the filters keep a repository: archived ones and forks are left out unless asked."""
    if raw.get("archived") and not _flag(filters, "include_archived"):
        return False
    is_fork = bool(raw.get("fork") or raw.get("forked_from_project"))
    return not (is_fork and not _flag(filters, "include_forks"))


def github_sbom_software(document: Any, *, repository: str = "") -> list[dict[str, Any]]:
    """The packages of a GitHub dependency graph SBOM (SPDX) as software entries, the repository itself left out."""
    from app.services import sbom_parser

    doc = document.get("sbom") if isinstance(document, dict) and "sbom" in document else document
    sbom = sbom_parser.parse(doc)
    own = repository.strip().lower()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for component in sbom.components:
        product = _text(component.product, 300)
        if not product:
            continue
        purl = component.purl or ""
        if own and (product.lower() == own or purl.lower().startswith(f"pkg:github/{own}@")):
            continue  # the package GitHub writes for the repository itself
        version = _text(component.version, 100) or "unknown"
        out.setdefault(
            (product.lower(), version.lower(), purl.lower()),
            {
                "vendor": _text(component.vendor, 200),
                "product": product,
                "version": version,
                "cpe": _text(component.cpe, 500),
                "purl": _text(purl, 500),
            },
        )
    return list(out.values())[:MAX_DEPENDENCIES]


def gitlab_purl(dependency: dict[str, Any]) -> str:
    """A PURL for a GitLab dependency (empty when its package manager has no PURL type)."""
    kind = _PURL_TYPES.get(_text(dependency.get("package_manager"), 40).lower())
    name = _text(dependency.get("name"), 300)
    if not kind or not name:
        return ""
    if kind == "maven" and ":" in name:
        name = name.replace(":", "/", 1)
    version = _text(dependency.get("version"), 100)
    path = "/".join(quote(part, safe="@.-_") for part in name.split("/"))
    return f"pkg:{kind}/{path}" + (f"@{quote(version, safe='.-_+')}" if version else "")


def gitlab_dependency_software(dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GitLab dependency list entries as software entries (``version`` ``unknown`` when GitLab gives none)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for dep in dependencies:
        product = _text(dep.get("name"), 300)
        if not product:
            continue
        version = _text(dep.get("version"), 100) or "unknown"
        out.setdefault(
            (product.lower(), version.lower()),
            {"vendor": "", "product": product, "version": version, "cpe": "", "purl": gitlab_purl(dep)},
        )
    return list(out.values())[:MAX_DEPENDENCIES]


def gitlab_dependency_findings(
    dependencies: list[dict[str, Any]], *, project: dict[str, Any], host: str
) -> list[dict[str, Any]]:
    """The vulnerabilities GitLab lists on a project's dependencies, one finding each (#1331, slice 3).

    The dependency list answers them when the token may read the project's
    security dashboard; each one becomes a path 3 signal on the repository
    asset (:mod:`app.services.ctem_code_findings`). Keyed ``<project id>:<vulnerability id>``.
    """
    project_id = _text(project.get("external_id"), 40)
    if not project_id:
        return []
    out: dict[str, dict[str, Any]] = {}
    for dep in dependencies:
        package = _text(dep.get("name"), 300)
        vulns = dep.get("vulnerabilities")
        if not package or not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict) or vuln.get("id") in (None, ""):
                continue
            external_id = f"{project_id}:{_text(vuln.get('id'), 40)}"
            out.setdefault(
                external_id,
                {
                    "external_id": external_id,
                    "package": package,
                    "version": _text(dep.get("version"), 100) or "unknown",
                    "purl": gitlab_purl(dep),
                    "manifest_path": _text(dep.get("dependency_file_path"), 300),
                    "name": _text(vuln.get("name"), 300),
                    "severity": _text(vuln.get("severity"), 20).lower(),
                    "url": _text(vuln.get("url"), 500),
                    "repository": _text(project.get("name"), 300),
                    "repository_id": project_id,
                    "identity_key": repository_key(host, project_id),
                },
            )
    return list(out.values())


def next_link(link_header: str) -> str | None:
    """The ``rel="next"`` URL of an RFC 8288 ``Link`` header, else ``None``."""
    for part in (link_header or "").split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if any(s.strip() == 'rel="next"' for s in section[1:]):
            return section[0].strip().strip("<>") or None
    return None


def missing_scope_message(source: str, status: int) -> str:
    scopes = " and ".join(REQUIRED_SCOPES.get(source, ()))
    return f"{LABELS.get(source, source)} refused the read (HTTP {status}): grant the token {scopes}, then run again"


def not_connected_message(source: str) -> str:
    return f"no {LABELS.get(source, source)} integration is connected"


def no_token_message(source: str) -> str:
    return f"no token for the {LABELS.get(source, source)} integration: authorize it again"


# ── Connection (I/O) ──────────────────────────────────────────────────


async def connection_status(db: Any, user_id: str, source: str) -> tuple[bool, str, str]:
    """Whether *source* has an integration this administrator may read with (no API call)."""
    from app.services import organization_context_microsoft as microsoft

    integ = await microsoft._integration(db, user_id, source)
    if integ is None:
        return False, not_connected_message(source), REASON_NOT_CONNECTED
    if not microsoft.has_credential(integ):
        return False, no_token_message(source), REASON_NO_TOKEN
    return True, "", ""


async def access_token(db: Any, user_id: str, source: str) -> tuple[str | None, str, str]:
    """``(token, api_base, reason)`` for *source*: GitHub's REST API, or the GitLab instance's ``/api/v4``."""
    from app.integrations.http import parse_compound_token
    from app.integrations.providers.gitlab import gitlab_api_base
    from app.services import integration_tools as it
    from app.services import organization_context_microsoft as microsoft

    integ = await microsoft._integration(db, user_id, source)
    if integ is None:
        return None, "", not_connected_message(source)
    token = await it._get_token(db, str(integ.id), source, user_id=user_id)
    if not token:
        return None, "", no_token_message(source)
    if source == GITHUB:
        return token, GITHUB_API, ""
    base_url, secret = parse_compound_token(token, 2, greedy_last=True)
    if "||" not in token:
        base_url, secret = "", token
    api = gitlab_api_base(base_url)
    if not secret.strip() or api is None:
        return None, "", no_token_message(source)
    return secret.strip(), api, ""


def gitlab_host(api_base: str) -> str:
    """The host of a GitLab ``/api/v4`` base (``gitlab.com`` by default), lower-cased."""
    match = re.match(r"https?://([^/]+)", api_base or "")
    return match.group(1).lower() if match else "gitlab.com"


# ── Reading (I/O) ─────────────────────────────────────────────────────


@dataclass
class CodeRead:
    """What a code source answered: rows, the dependencies of each row, counts and the error that stopped it."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    software: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    total: int | None = None
    truncated: bool = False
    error: str | None = None
    software_rows: int = 0
    #: Repositories whose dependencies could not be read: their stored software is kept.
    software_unread: set[str] = field(default_factory=set)
    #: Repositories with no dependency list on the source, by reason code (:data:`SBOM_NOT_ENABLED`, ...).
    software_unavailable: dict[str, str] = field(default_factory=dict)
    #: Dependency vulnerabilities per repository (GitLab), for :mod:`app.services.ctem_code_findings`.
    findings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _headers(source: str, token: str) -> dict[str, str]:
    if source == GITHUB:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


async def _get(client: Any, url: str, headers: dict[str, str]) -> Any:
    """GET *url*, waiting out 429 and 503 with Retry-After (bounded); answers the last response."""
    resp = None
    for attempt in range(_RETRIES + 1):
        resp = await client.get(url, headers=headers)
        if resp.status_code in (429, 503) and attempt < _RETRIES:
            try:
                wait = float(resp.headers.get("Retry-After") or 2**attempt)
            except ValueError:
                wait = float(2**attempt)
            await asyncio.sleep(min(max(wait, 0.5), _MAX_WAIT_S))
            continue
        return resp
    return resp


def _next(resp: Any, url: str) -> str | None:
    link = next_link(resp.headers.get("Link") or resp.headers.get("link") or "")
    if link:
        return link
    page = (resp.headers.get("X-Next-Page") or resp.headers.get("x-next-page") or "").strip()
    if not page.isdigit():
        return None
    base, _, query = url.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith("page=")]
    return f"{base}?{'&'.join([*params, f'page={page}'])}"


async def _paged(
    client: Any, url: str, headers: dict[str, str], source: str, limit: int
) -> tuple[list[dict[str, Any]], bool, str]:
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        resp = await _get(client, next_url, headers)
        if resp.status_code in (401, 403):
            return items, False, missing_scope_message(source, resp.status_code)
        if resp.status_code >= 400:
            return items, False, f"{source}: HTTP {resp.status_code} on {next_url.split('?')[0]}"
        data = resp.json()
        page = data if isinstance(data, list) else []
        items.extend(v for v in page if isinstance(v, dict))
        if len(items) >= limit:
            return items[:limit], bool(_next(resp, next_url)) or len(items) > limit, ""
        next_url = _next(resp, next_url)
    return items, False, ""


_Listed = tuple[list[dict[str, Any]], bool, str]
#: ``(software, unavailable_reason, error, findings)``; findings ``None`` when the list was not read whole.
_Dependencies = tuple[list[dict[str, Any]], str, str, list[dict[str, Any]] | None]


async def _list_github(client: Any, api: str, headers: dict[str, str], filters: dict[str, Any], limit: int) -> _Listed:
    orgs = _names(filters, "organizations")
    if not orgs:
        return await _paged(
            client, f"{api}/user/repos?per_page={_PAGE}&affiliation=owner,organization_member", headers, GITHUB, limit
        )
    repos: list[dict[str, Any]] = []
    cut = False
    for org in orgs:
        page, org_cut, err = await _paged(
            client, f"{api}/orgs/{quote(org, safe='')}/repos?per_page={_PAGE}&type=all", headers, GITHUB, limit
        )
        if err:
            return repos, cut, err
        repos.extend(page)
        cut = cut or org_cut
    return repos[:limit], cut or len(repos) > limit, ""


async def _list_gitlab(client: Any, api: str, headers: dict[str, str], filters: dict[str, Any], limit: int) -> _Listed:
    archived = "" if _flag(filters, "include_archived") else "&archived=false"
    groups = _names(filters, "groups")
    if not groups:
        return await _paged(
            client, f"{api}/projects?membership=true&per_page={_PAGE}&order_by=id{archived}", headers, GITLAB, limit
        )
    projects: list[dict[str, Any]] = []
    cut = False
    for group in groups:
        page, group_cut, err = await _paged(
            client,
            f"{api}/groups/{quote(group, safe='')}/projects?include_subgroups=true&per_page={_PAGE}&order_by=id{archived}",
            headers,
            GITLAB,
            limit,
        )
        if err:
            return projects, cut, err
        projects.extend(page)
        cut = cut or group_cut
    return projects[:limit], cut or len(projects) > limit, ""


async def _github_dependencies(client: Any, api: str, headers: dict[str, str], row: dict[str, Any]) -> _Dependencies:
    """``(software, unavailable_reason, error, findings)`` of one GitHub repository.

    GitHub findings come as Dependabot alert deliveries (``ctem_signal_listeners``), not here.
    """
    from app.services import sbom_parser

    path = "/".join(quote(part, safe="") for part in row["name"].split("/", 1))
    resp = await _get(client, f"{api}/repos/{path}/dependency-graph/sbom", headers)
    if resp.status_code == 404:
        return [], SBOM_NOT_ENABLED, "", None
    if resp.status_code >= 400:
        return [], "", f"HTTP {resp.status_code}", None
    try:
        return github_sbom_software(resp.json(), repository=row["name"]), "", "", None
    except (sbom_parser.SbomError, ValueError) as exc:
        return [], "", str(exc)[:200], None


async def _gitlab_dependencies(client: Any, api: str, headers: dict[str, str], row: dict[str, Any]) -> _Dependencies:
    """``(software, unavailable_reason, error, findings)`` of one GitLab project (dependency list: GitLab Ultimate)."""
    url = f"{api}/projects/{quote(row['external_id'], safe='')}/dependencies?per_page={_PAGE}"
    deps, cut, err = await _paged(client, url, headers, GITLAB, MAX_DEPENDENCIES)
    if err and ("HTTP 403" in err or "HTTP 404" in err):
        return [], SBOM_TIER, "", None
    if err:
        return [], "", err, None
    # A cut list is not the project's whole list: its findings wait for a whole read,
    # so a vulnerability past the cut never reads as closed.
    findings = None if cut else gitlab_dependency_findings(deps, project=row, host=gitlab_host(api))
    return gitlab_dependency_software(deps), "", "", findings


async def read_repositories(
    source: str, token: str, api: str, filters: dict[str, Any], *, limit: int, client: Any = None
) -> CodeRead:
    """The repositories of *source* the filters keep, and their dependencies (``limit`` repositories at most)."""
    from app.core.egress import egress_client

    result = CodeRead()
    headers = _headers(source, token)
    own = client is None
    http = client or egress_client(timeout=60)
    try:
        if own:
            http = await http.__aenter__()
        lister = _list_github if source == GITHUB else _list_gitlab
        raw, cut, err = await lister(http, api, headers, filters, limit)
        if err:
            result.error = err
            return result
        host = GITHUB_HOST if source == GITHUB else gitlab_host(api)
        for repo in raw:
            row = github_repo_row(repo, host=host) if source == GITHUB else gitlab_project_row(repo, host=host)
            if row["external_id"] and keep_repository(row, repo, filters):
                result.rows.append(row)
        result.total, result.truncated = len(result.rows), cut
        if _flag(filters, "skip_dependencies"):
            return result
        reader = _github_dependencies if source == GITHUB else _gitlab_dependencies
        sem = asyncio.Semaphore(_SBOM_CONCURRENCY)

        async def deps_of(row: dict[str, Any]) -> None:
            async with sem:
                software, unavailable, dep_err, findings = await reader(http, api, headers, row)
            if dep_err:
                logger.info("%s dependencies of %s unreadable: %s", LABELS[source], row["external_id"], dep_err)
                result.software_unread.add(row["external_id"])
                return
            if unavailable:
                result.software_unavailable[row["external_id"]] = unavailable
            result.software[row["external_id"]] = software
            result.software_rows += len(software)
            if findings is not None:
                # Read whole: the findings it lists are the project's current ones (an empty list closes).
                result.findings[row["external_id"]] = findings

        await asyncio.gather(*(deps_of(r) for r in result.rows))
        return result
    except Exception as exc:  # reported, never guessed
        logger.warning("%s read failed: %s", LABELS.get(source, source), format_exception_message(exc))
        result.error = f"{source}: {type(exc).__name__}"
        return result
    finally:
        if own:
            await http.__aexit__(None, None, None)


async def owner_options(db: Any, user_id: str, source: str) -> list[dict[str, str]]:
    """The organizations (GitHub) or groups (GitLab) the account reaches, as filter options."""
    from app.core.egress import egress_client

    token, api, _reason = await access_token(db, user_id, source)
    if not token:
        return []
    headers = _headers(source, token)
    url = (
        f"{api}/user/orgs?per_page={_PAGE}"
        if source == GITHUB
        else f"{api}/groups?per_page={_PAGE}&min_access_level=10"
    )
    async with egress_client(timeout=30) as http:
        items, _cut, err = await _paged(http, url, headers, source, 500)
    if err:
        raise RuntimeError(err)
    key = "login" if source == GITHUB else "full_path"
    return [
        {
            "value": _text(i.get(key)),
            "label": _text(i.get(key)),
            "description": _text(i.get("description")),
            "parent": "",
        }
        for i in items
        if _text(i.get(key))
    ]
