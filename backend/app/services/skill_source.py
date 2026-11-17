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

"""Authenticated skill sources — private GitHub repositories.

A URL-based skill (``AgentSkill.url``) is normally fetched with a single
unauthenticated ``GET``.  That works for public ``SKILL.md`` files but always
fails for a file living in a **private** repository: GitHub answers 401 (bad
credentials) or 404 (invisible resource) to an anonymous request, and the skill
eventually lands in the auto-refresh quarantine.

This module adds the missing authenticated path.  A skill may bind a GitHub
:class:`~app.models.integration.Integration` (``AgentSkill.source_integration_id``);
when it does, the fetch is routed through the GitHub *contents* API with an
``Authorization`` header built from that integration's already-encrypted
credential, so **no new secret store is introduced** — the platform reuses the
same per-integration Fernet-encrypted credential every GitHub tool uses.  Both
of the GitHub integration's auth types are accepted: ``api_key`` (a classic or
fine-grained Personal Access Token) and ``oauth`` (an OAuth App / GitHub App
user token, re-read on every refresh so rotation is transparent).

Design notes
------------

**Branch-HEAD tracking, not commit pinning.** ``GET /repos/{owner}/{repo}/contents/{path}``
without a ``ref`` parameter returns the file as of the *default branch's HEAD*,
which is exactly the "always the latest merged version" semantics the customer
workflow (PR review → merge to default branch) needs — and it costs no extra
API round-trip to resolve the default branch name.  A ``ref`` is sent only when
the source URL carries one (``/blob/main/…``) or the skill pins one explicitly
via ``AgentSkill.source_ref``.  Pinning a commit SHA is therefore possible but
never implicit.

**Failure classification.** GitHub overloads its status codes: 403 means both
"forbidden" and "rate limit exhausted", and 404 means both "path is gone" and
"your credential cannot see this repository".  :func:`classify_github_failure`
and :func:`diagnose_not_found` separate these so the refresh scheduler can tell
an expired token (recoverable by rotating the credential — must never be
quarantined as a permanently broken URL) apart from a genuinely dead path
(quarantine) apart from a provider outage.

**Secrets never reach a log record.** The credential travels in an
``Authorization`` request header, which is never logged here, and callers log
URLs through ``web_fetch._redact_url_for_log``.  Nothing in this module logs a
request object, a header mapping, or a token.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import EgressRoute, egress_client, egress_route
from app.core.security import decrypt_value
from app.integrations.providers.github import GITHUB_API
from app.models.integration import Integration
from app.services.integration_user_credentials import is_per_user
from app.services.vault import SecretUnavailableError, reveal_field

__all__ = [
    "GITHUB_SKILL_PROVIDER",
    "SKILL_CONTENT_MAX_BYTES",
    "GitHubSkillSource",
    "SkillCredential",
    "SkillCredentialError",
    "classify_github_failure",
    "contents_api_url",
    "diagnose_not_found",
    "fetch_skill_content",
    "frontmatter_bounds",
    "github_auth_headers",
    "parse_github_skill_url",
    "parse_skill_frontmatter",
    "repo_api_url",
    "resolve_skill_credential",
    "split_skill_frontmatter",
]

#: Only GitHub is supported as an authenticated skill source (GitLab / Bitbucket are
#: explicitly out of scope).
GITHUB_SKILL_PROVIDER = "github"

#: Pinned REST API version, mirroring ``providers/github/_common._API_VERSION``
#: so a future default bump cannot silently change payload shapes.
_API_VERSION = "2022-11-28"

_GITHUB_WEB_HOSTS = frozenset({"github.com", "www.github.com"})
_GITHUB_RAW_HOST = "raw.githubusercontent.com"
_GITHUB_API_HOST = "api.github.com"

#: Segment inserted by GitHub's web UI between ``owner/repo`` and ``ref/path``.
#: ``tree`` is deliberately absent — it addresses a *directory*, and the
#: contents endpoint answers a directory with a JSON listing rather than file
#: bytes, which would cache a listing as the skill body.
_WEB_REF_MARKERS = frozenset({"blob", "raw", "blame"})

#: Fully-qualified ref prefixes that ``raw.githubusercontent.com`` accepts
#: (``/refs/heads/main/…``).  Consumed as a unit so a branch whose name
#: contains a ``/`` still resolves when written in the fully-qualified form.
_REF_NAMESPACES: tuple[tuple[str, ...], ...] = (
    ("refs", "heads"),
    ("refs", "tags"),
    ("refs", "remotes"),
)


@dataclass(frozen=True, slots=True)
class GitHubSkillSource:
    """A ``SKILL.md`` file located inside a GitHub repository.

    ``ref`` is ``None`` when the source URL did not name one — the fetch then
    tracks the repository's default-branch HEAD (see the module docstring).
    """

    owner: str
    repo: str
    path: str
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class SkillCredential:
    """A resolved, ready-to-use credential for an authenticated skill fetch.

    ``token`` is decrypted in memory only; ``integration_name`` /
    ``integration_id`` exist so failures can be attributed to a specific
    integration row in logs without ever naming the secret.
    """

    token: str
    integration_id: uuid.UUID
    integration_name: str
    # The integration's relay route: a skill read through a GitHub
    # integration routed through a relay group is fetched through that group.
    route: EgressRoute | None = None


class SkillCredentialError(Exception):
    """The bound integration cannot supply a usable credential.

    Raised for an integration that is missing, of the wrong provider, not
    connected, or holds no token — i.e. every case an operator must fix before
    the fetch can possibly succeed.
    """


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def _split_ref_and_path(
    segments: list[str], *, known_ref: str | None = None
) -> tuple[str | None, str]:
    """Split ``[ref..., path...]`` segments into ``(ref, path)``.

    GitHub URLs are inherently ambiguous here: in
    ``/blob/release/v2/docs/SKILL.md`` the ref could be ``release`` (path
    ``v2/docs/SKILL.md``) or ``release/v2`` (path ``docs/SKILL.md``).  Three
    rules resolve it, in order:

    1. *known_ref* (an explicit ``AgentSkill.source_ref``) wins when the
       segments actually start with it — the only way to be certain about a
       branch name containing a ``/``.
    2. A fully-qualified ``refs/heads/<name>`` prefix is consumed as a unit.
    3. Otherwise the first segment is the ref — the same assumption GitHub's
       own web UI makes when it generates these links.
    """
    if known_ref:
        ref_segments = known_ref.split("/")
        for namespace in ((), *_REF_NAMESPACES):
            end = len(namespace) + len(ref_segments)
            if (
                len(segments) > end
                and tuple(segments[: len(namespace)]) == namespace
                and segments[len(namespace) : end] == ref_segments
            ):
                return known_ref, "/".join(segments[end:])

    for namespace in _REF_NAMESPACES:
        end = len(namespace) + 1
        if len(segments) > end and tuple(segments[: len(namespace)]) == namespace:
            return segments[len(namespace)], "/".join(segments[end:])

    if len(segments) < 2:
        return None, "/".join(segments)
    return segments[0], "/".join(segments[1:])


def parse_github_skill_url(
    url: str | None, *, ref_override: str | None = None
) -> GitHubSkillSource | None:
    """Parse *url* into a :class:`GitHubSkillSource`, or ``None``.

    Recognises every shape a user realistically pastes into the skill URL
    field:

    - ``https://github.com/{owner}/{repo}/blob/{ref}/{path}`` (web UI link,
      also ``/raw/`` and ``/blame/``)
    - ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``
      (including the ``refs/heads/{branch}`` form)
    - ``https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}``

    Returns ``None`` for anything else (a non-GitHub host, a repository root,
    a Gist, GitHub Enterprise Server) so callers can fall back to the plain
    unauthenticated ``GET`` and public skills keep working unchanged.

    *ref_override* only disambiguates the ref/path split; the returned ``ref``
    still reflects what the URL itself carries when no override matched, and
    callers decide which one wins (see :func:`contents_api_url`).
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    segments = [seg for seg in parsed.path.split("/") if seg]

    if host in _GITHUB_WEB_HOSTS:
        # /{owner}/{repo}/{blob|raw|blame|tree}/{ref}/{path...}
        if len(segments) < 5 or segments[2] not in _WEB_REF_MARKERS:
            return None
        owner, repo = segments[0], segments[1]
        ref, path = _split_ref_and_path(segments[3:], known_ref=ref_override)
    elif host == _GITHUB_RAW_HOST:
        # /{owner}/{repo}/{ref}/{path...}
        if len(segments) < 4:
            return None
        owner, repo = segments[0], segments[1]
        ref, path = _split_ref_and_path(segments[2:], known_ref=ref_override)
    elif host == _GITHUB_API_HOST:
        # /repos/{owner}/{repo}/contents/{path...}?ref={ref}
        if len(segments) < 5 or segments[0] != "repos" or segments[3] != "contents":
            return None
        owner, repo = segments[1], segments[2]
        path = "/".join(segments[4:])
        ref_values = parse_qs(parsed.query).get("ref") or []
        ref = ref_values[0] or None if ref_values else None
    else:
        return None

    # ``.git`` suffixes appear in clone URLs pasted by mistake; strip so the
    # API path is valid instead of failing with a confusing 404.
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo or not path:
        return None
    return GitHubSkillSource(owner=owner, repo=repo, path=path, ref=ref)


def contents_api_url(source: GitHubSkillSource, *, ref: str | None = None) -> str:
    """Build the GitHub contents-API URL for *source*.

    An explicit *ref* (from ``AgentSkill.source_ref``) wins over the one parsed
    from the URL.  When neither is present the ``ref`` parameter is omitted so
    GitHub serves the repository's **default-branch HEAD** — the
    "latest merged version" contract.
    """
    effective_ref = ref or source.ref
    # ``owner``/``repo``/``path`` are interpolated into a request URL: encode
    # them so a crafted value cannot inject a new path segment or query.
    base = (
        f"{GITHUB_API}/repos/{quote(source.owner, safe='')}"
        f"/{quote(source.repo, safe='')}/contents/{quote(source.path, safe='/')}"
    )
    if effective_ref:
        return f"{base}?ref={quote(effective_ref, safe='')}"
    return base


def repo_api_url(source: GitHubSkillSource) -> str:
    """Build the GitHub repository-metadata URL for *source*."""
    return (
        f"{GITHUB_API}/repos/{quote(source.owner, safe='')}"
        f"/{quote(source.repo, safe='')}"
    )


def github_auth_headers(token: str) -> dict[str, str]:
    """Headers for an authenticated raw-content read of a repository file.

    ``application/vnd.github.v3.raw`` makes the contents endpoint return the
    file bytes directly instead of a base64-wrapped JSON envelope, so the
    response body can feed straight into ``parse_skill_frontmatter``.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.raw",
        "X-GitHub-Api-Version": _API_VERSION,
    }


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

#: Outcome labels shared by the refresh scheduler's logging and back-off logic.
#:   ``auth``       – credential is missing, expired, revoked or under-scoped
#:   ``rate_limit`` – throttled / quota exhausted (transient, provider-side)
#:   ``not_found``  – the path itself is gone (permanent without reconfiguration)
#:   ``client``     – any other 4xx (permanent without reconfiguration)
#:   ``transient``  – 5xx / timeout (retry soon)
FailureKind = str


def classify_github_failure(
    status_code: int, headers: Mapping[str, str] | None = None
) -> FailureKind:
    """Classify an HTTP failure status from a skill fetch.

    GitHub answers a spent rate limit with **403** (not only 429), and marks it
    with ``x-ratelimit-remaining: 0`` or a ``retry-after`` header.  Treating
    that as an auth failure would blame the operator's token for a throttle,
    so the headers are inspected before 401/403 is called an auth problem.
    """
    hdrs = headers or {}
    # httpx response headers are case-insensitive, but a plain dict from a test
    # or another caller is not — normalise so both work.
    remaining = hdrs.get("x-ratelimit-remaining", hdrs.get("X-RateLimit-Remaining"))
    retry_after = hdrs.get("retry-after", hdrs.get("Retry-After"))

    if status_code in (401, 403):
        if remaining == "0" or retry_after:
            return "rate_limit"
        return "auth"
    if status_code in (408, 429):
        return "rate_limit"
    if status_code == 404:
        return "not_found"
    if status_code >= 500:
        return "transient"
    return "client"


async def diagnose_not_found(
    client: Any, source: GitHubSkillSource, headers: Mapping[str, str]
) -> FailureKind:
    """Decide whether a 404 on an authenticated fetch is auth- or path-related.

    GitHub hides resources a credential cannot see behind 404 rather than 403,
    so "file renamed" and "token lost access to the repository" are
    indistinguishable from the file request alone.  One extra probe of the
    repository endpoint separates them:

    - repository readable  → the *file path* is genuinely gone (``not_found``,
      so the quarantine still protects the daily HTTP budget)
    - 401/403              → the credential itself is rejected (``auth``)
    - anything else        → ``not_found``, keeping today's behaviour rather
      than inventing a diagnosis (the caller's log names the ambiguity)

    Never raises: a failed probe degrades to ``not_found``.
    """
    try:
        resp = await client.get(repo_api_url(source), headers=dict(headers))
    except Exception:
        return "not_found"
    status = getattr(resp, "status_code", None)
    if status == 200:
        return "not_found"
    if status in (401, 403):
        return classify_github_failure(status, getattr(resp, "headers", None))
    return "not_found"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


async def resolve_skill_credential(
    db: AsyncSession,
    integration_id: uuid.UUID | str | None,
    *,
    subject: str = "skill",
    rebind_hint: str = "in the skill's catalog settings",
) -> SkillCredential | None:
    """Load and decrypt the credential bound to a skill, or ``None``.

    *subject* and *rebind_hint* word the errors for another unattended reader
    of a private repository that binds a GitHub integration the same way (the
    brand asset source, ``services/brand_asset_source.py``).

    Returns ``None`` when no integration is bound (the skill is public and
    fetched anonymously, exactly as before this feature).  Raises
    :class:`SkillCredentialError` when an integration *is* bound but cannot
    produce a token, so the caller reports a configuration problem instead of
    silently downgrading to an anonymous request that would 404 on a private
    repository.

    Unlike ``kb_sync._get_integration_token`` there is deliberately **no
    auto-discovery fallback** to "any connected GitHub integration": a skill is
    a shared entity whose refresh runs unattended in the scheduler, so the
    credential it uses must be the one an operator explicitly chose and can
    audit — never one silently substituted at fetch time.
    """
    if not integration_id:
        return None
    try:
        as_uuid = uuid.UUID(str(integration_id))
    except (TypeError, ValueError) as exc:
        raise SkillCredentialError(
            f"The {subject}'s source credential reference is not a valid integration id."
        ) from exc

    result = await db.execute(select(Integration).where(Integration.id == as_uuid))
    integration = result.scalar_one_or_none()
    if integration is None:
        raise SkillCredentialError(
            f"The GitHub integration bound to this {subject} no longer exists. "
            f"Attach another one {rebind_hint}."
        )
    if integration.provider != GITHUB_SKILL_PROVIDER:
        raise SkillCredentialError(
            f"Integration '{integration.name}' is a {integration.provider} "
            f"integration; {subject} sources can only authenticate against GitHub."
        )
    if integration.status != "connected":
        raise SkillCredentialError(
            f"GitHub integration '{integration.name}' is not connected "
            f"(status: {integration.status}). Re-authorize it in Integrations."
        )
    # A skill is shared and refreshed unattended: there is no one person whose
    # own account it would act with.
    if is_per_user(integration):
        raise SkillCredentialError(
            f"GitHub integration '{integration.name}' acts with each person's own "
            f"account, so it cannot authenticate a {subject}'s source, which is shared "
            "and refreshed unattended. Attach an integration that shares its account."
        )

    # Both GitHub integration auth types work here, because both end up as a
    # bearer token on the REST API:
    #   - ``oauth``   -> ``access_token_enc``: an OAuth App / GitHub App user
    #     token.  Read fresh on every refresh, so the platform's token-refresh
    #     sweep rotating it is picked up automatically.
    #   - ``api_key`` -> ``api_key_enc``: a Personal Access Token (classic or
    #     fine-grained).  A fine-grained PAT must list the repository and grant
    #     "Contents: Read".
    # OAuth first, mirroring ``kb_sync._get_integration_token`` so an
    # integration holding both resolves the same way everywhere.
    if integration.access_token_enc:
        token = decrypt_value(integration.access_token_enc)
    else:
        try:
            token = await reveal_field(integration, "api_key_enc")
        except SecretUnavailableError as exc:
            raise SkillCredentialError(str(exc)) from None
    if not token:
        raise SkillCredentialError(
            f"GitHub integration '{integration.name}' holds no access token or "
            "API key. Re-authorize it (OAuth / GitHub App) or set a Personal "
            "Access Token in Integrations."
        )
    from app.services.relays import integration_egress_route

    return SkillCredential(
        token=token,
        integration_id=integration.id,
        integration_name=integration.name,
        route=await integration_egress_route(db, integration),
    )


# ---------------------------------------------------------------------------
# Skill content fetching + YAML frontmatter parsing, shared by the skills API,
# the agent builder and the scheduler's refresh
# ---------------------------------------------------------------------------

SKILL_CONTENT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


def frontmatter_bounds(raw: str) -> tuple[int, int, int] | None:
    """Locate the YAML frontmatter ``---`` delimiters of a SKILL.md, without regex.

    Returns ``(fm_start, close_start, body_start)`` offsets, or *None* when
    there is no frontmatter: ``fm_start`` is right after the opening ``---``
    line, ``close_start`` at the start of the closing ``---`` line and
    ``body_start`` right after it. A delimiter line is ``---`` followed by
    nothing but whitespace, a CR included, so a CRLF file has frontmatter
    too; a ``---something`` line inside the block does not close it.
    """
    if not raw.startswith("---"):
        return None
    first_nl = raw.find("\n")
    if first_nl < 0:
        return None
    if raw[:first_nl].strip() != "---":
        return None
    fm_start = first_nl + 1
    pos = fm_start
    while True:
        idx = raw.find("\n---", pos)
        if idx < 0:
            return None
        after = idx + 4  # skip "\n---"
        end_of_line = raw.find("\n", after)
        trailing = raw[after:] if end_of_line < 0 else raw[after:end_of_line]
        if trailing.strip() == "":
            body_start = end_of_line + 1 if end_of_line >= 0 else len(raw)
            return fm_start, idx + 1, body_start
        pos = idx + 1


async def _validate_skill_url(url: str) -> str:
    """Validate URL for SSRF and return a sanitized copy.

    Ensures scheme is http/https and hostname does not resolve to private,
    loopback, or link-local addresses.  Returns a reconstructed URL so
    callers use the validated value directly.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="Skill URL must use http or https scheme.",
        )
    host = parsed.hostname
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Skill URL must have a valid hostname.",
        )
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        raise HTTPException(
            status_code=400,
            detail="Skill URL hostname could not be resolved.",
        ) from None
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise HTTPException(
                    status_code=400,
                    detail="Skill URL must not point to private or internal addresses.",
                )
        except ValueError:
            continue

    from urllib.parse import urlunparse

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def parse_skill_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """:func:`split_skill_frontmatter` of a skill a person or an agent submits.

    Refuses content over :data:`SKILL_CONTENT_MAX_BYTES` with a 400, the limit
    of a skill written through the API, the agent builder or a URL refresh.
    """
    if len(raw.encode("utf-8")) > SKILL_CONTENT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Skill content exceeds maximum size of {SKILL_CONTENT_MAX_BYTES // (1024 * 1024)} MB.",
        )
    return split_skill_frontmatter(raw)


def split_skill_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Extract YAML frontmatter from a SKILL.md file: the one parser.

    Returns ``(metadata_dict, body)`` where *body* is the markdown content
    after the frontmatter block.  If no frontmatter is found, the entire
    text is returned as the body.

    Supports YAML block scalars (``>`` / ``>-`` / ``>+`` folded, ``|`` /
    ``|-`` / ``|+`` literal) so that multi-line descriptions are correctly
    captured; a block ends at the first line indented no deeper than its key.
    No size limit: a skill package's files carry their own
    (``skill_package_manager``).
    """
    bounds = frontmatter_bounds(raw)
    if bounds is None:
        return {}, raw

    fm_start, close_start, body_start = bounds
    body = raw[body_start:]
    meta: dict[str, str] = {}

    lines = raw[fm_start:close_start].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if value in _BLOCK_SCALAR_INDICATORS:
            # The block is every following line indented deeper than its key,
            # blank lines included; the first line indented no deeper is the
            # next key, read on the next pass.
            indent = _indentation(line)
            block_lines: list[str] = []
            while i < len(lines) and (not lines[i].strip() or _indentation(lines[i]) > indent):
                block_lines.append(lines[i].strip())
                i += 1
            value = _literal_block(block_lines) if value.startswith("|") else _folded_block(block_lines)
        if key and value:
            meta[key] = value
    return meta, body


#: YAML block scalar indicators: literal ``|`` and folded ``>``, with or without
#: a chomping indicator (strip ``-``, keep ``+``). A frontmatter value is
#: stripped whole, so chomping changes nothing in what is read.
_BLOCK_SCALAR_INDICATORS = frozenset({"|", "|-", "|+", ">", ">-", ">+"})


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _literal_block(lines: list[str]) -> str:
    """A literal block: its lines, one per line."""
    return "\n".join(lines).strip()


def _folded_block(lines: list[str]) -> str:
    """A folded block: lines joined by spaces, a blank line a paragraph break."""
    parts: list[str] = []
    paragraph: list[str] = []
    for line in lines:
        if line:
            paragraph.append(line)
        elif paragraph:
            parts.append(" ".join(paragraph))
            paragraph = []
    if paragraph:
        parts.append(" ".join(paragraph))
    return "\n".join(parts).strip()


async def fetch_skill_content(
    url: str,
    *,
    credential: SkillCredential | None = None,
    source_ref: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Fetch a SKILL.md from *url* and return ``(raw_content, frontmatter)``.

    Without a *credential* this is the historical single anonymous ``GET``.
    With one, the read is routed through the authenticated GitHub contents API
    so a file in a **private** repository resolves, tracking
    *source_ref* — or the repository's default-branch HEAD when it is unset.
    """
    safe_url = await _validate_skill_url(url)
    headers: dict[str, str] | None = None
    request_url = safe_url

    if credential is not None:
        source = parse_github_skill_url(safe_url, ref_override=source_ref)
        if source is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A GitHub credential can only be attached to a GitHub "
                    "repository file URL (github.com/<owner>/<repo>/blob/…, "
                    "raw.githubusercontent.com/…, or the contents API). "
                    "Remove the credential, or point the URL at a SKILL.md "
                    "inside a GitHub repository."
                ),
            )
        request_url = contents_api_url(source, ref=source_ref)
        headers = github_auth_headers(credential.token)

    async with egress_client(timeout=30, follow_redirects=True) as client:
        with egress_route(credential.route if credential is not None else None):
            resp = await client.get(request_url, headers=headers)
        if headers is not None and resp.status_code >= 400:
            _raise_github_fetch_error(resp, credential)
        resp.raise_for_status()
        if len(resp.content) > SKILL_CONTENT_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Skill content exceeds maximum size of {SKILL_CONTENT_MAX_BYTES // (1024 * 1024)} MB.",
            )
    raw = resp.text
    meta, _ = parse_skill_frontmatter(raw)
    return raw, meta


def _raise_github_fetch_error(
    resp: httpx.Response, credential: SkillCredential | None
) -> None:
    """Turn a failed authenticated GitHub read into an actionable 4xx.

    The point of validating at create/update time is that the operator learns
    *which* thing is wrong now, instead of finding a quarantined skill later —
    so an expired token, a token that cannot see the repository, and a wrong
    path each get their own message.  The token is never echoed.
    """
    name = credential.integration_name if credential else "unknown"
    kind = classify_github_failure(resp.status_code, resp.headers)
    if kind == "auth":
        raise HTTPException(
            status_code=422,
            detail=(
                f"GitHub rejected the credential from integration '{name}' "
                f"(HTTP {resp.status_code}). Re-authorize the integration, or "
                "use a token that is still valid."
            ),
        )
    if kind == "rate_limit":
        raise HTTPException(
            status_code=502,
            detail=(
                "GitHub is currently throttling requests for integration "
                f"'{name}' (HTTP {resp.status_code}). Try again shortly."
            ),
        )
    if kind == "not_found":
        raise HTTPException(
            status_code=422,
            detail=(
                "GitHub returned 404 for this file. Either the path does not "
                f"exist on the tracked branch, or the credential from '{name}' "
                "does not grant access to the repository — a private repository "
                "is indistinguishable from a missing one to a token that cannot "
                "see it. Check the path and the integration's repository access."
            ),
        )
    if kind == "transient":
        raise HTTPException(
            status_code=502,
            detail=f"GitHub returned HTTP {resp.status_code}. Try again shortly.",
        )
    raise HTTPException(
        status_code=422,
        detail=f"Failed to fetch the skill from GitHub: HTTP {resp.status_code}.",
    )
