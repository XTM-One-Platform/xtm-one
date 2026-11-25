"""GitHub integration - shared helpers, constants, HTTP + GraphQL, formatters.

The GitHub surface is split into a per-domain package (mirroring
``providers/drata`` and ``providers/opencti``): ``_common`` holds the shared
HTTP/GraphQL plumbing, constants and formatters, and one module per domain
(orgs, repos, contents, commits, branches, issues, pulls, actions, checks,
security, releases, projects, search, users, graphql) registers its own tools.
``__init__.build_tools`` wires every domain's ``register()`` together.

Every domain module performs HTTP through ``_common.safe_request`` and GraphQL
through ``_common.gh_graphql`` so a single ``monkeypatch.setattr(_common,
"safe_request", ...)`` (or patch of ``app.integrations.providers.github._common``)
covers every tool in tests - the same convention the Drata package uses.

Docs:
  - REST:    https://docs.github.com/en/rest
  - GraphQL: https://docs.github.com/en/graphql
  - API versioning: https://docs.github.com/en/rest/about-the-rest-api/api-versions
"""

from __future__ import annotations

import html
import re
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    format_entity_list,
    format_pagination_hint,
)
from app.integrations.http import safe_request as _http_safe_request

__all__ = [
    "GITHUB_API",
    "format_entity_list",
    "format_pagination_hint",
    "gh_graphql",
    "safe_request",
]

_PROVIDER = "GitHub"
_BASE_URL = "https://api.github.com"
#: Public alias imported by oauth_callbacks / integration_testing /
#: integration_github_webhooks / kb_sync / integration_elements. Keep exported
#: from the package ``__init__`` so those call sites never break.
GITHUB_API = _BASE_URL
_GRAPHQL_URL = f"{_BASE_URL}/graphql"

#: Pin the REST API version so a future default bump can't silently change
#: payload shapes (https://docs.github.com/en/rest/about-the-rest-api/api-versions).
#: Sub-issues + issue-types endpoints are available under this version.
_API_VERSION = "2022-11-28"

#: Truncation caps to keep LLM context manageable.
_MAX_DIFF_CHARS = 80_000
_MAX_GRAPHQL_CHARS = 80_000
_MAX_LOG_CHARS = 40_000
_MAX_BODY_PREVIEW = 200
#: A PR or issue read shows this much of its body unless the caller asks for it.
_BODY_HEAD_CHARS = 1_500
#: A review or comment line of a default PR or issue read.
_ITEM_PREVIEW_CHARS = 240
#: The window ``include_body=true`` reads by default, and its cap.
_DEFAULT_BODY_WINDOW = 20_000
_MAX_BODY_WINDOW = 60_000

# Status / conclusion icon maps (shared across CI/CD, Checks and Actions tools).
_CONCLUSION_ICONS: dict[str, str] = {
    "success": "✓",
    "failure": "✗",
    "neutral": "—",
    "cancelled": "⊘",
    "skipped": "⊘",
    "timed_out": "✗",
    "action_required": "!",
    "stale": "—",
    "in_progress": "⟳",
    "queued": "◯",
    "pending": "◯",
    "waiting": "◯",
    "requested": "◯",
    "error": "✗",
    "completed": "✓",
}

# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    """Standard authenticated GitHub REST headers (with pinned API version)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
    }


def _json_headers(headers: dict[str, str]) -> dict[str, str]:
    """Add ``Content-Type: application/json`` for write requests."""
    return {**headers, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Repository / argument validation
# ---------------------------------------------------------------------------

# owner/name segment validation. The owner login allows letters, digits and
# hyphens; the repository name additionally allows '.' and '_'. The repo value
# is interpolated raw into request paths, so these strict allow-lists block
# path/query injection (``../``, ``?``, ``#``, whitespace, ``%2F`` ...).
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Template-style placeholder tokens ({{variable}}, <variable>, [variable]).
# An LLM sometimes emits these verbatim when a prompt variable was not
# substituted, so we detect them before the value reaches an API URL.
# HTML-escaped forms (&lt;variable&gt;, doubly-escaped &amp;lt;variable&amp;gt;,
# numeric &#60;variable&#62;, ...) are handled by _is_template_token(), which
# unescapes entities iteratively before matching this pattern.
_PLACEHOLDER_TOKEN_RE = re.compile(r"^\{\{.+\}\}$|^<[^>]+>$|^\[[^\]]+\]$")

#: Upper bound on iterative HTML-entity unescape rounds in
#: :func:`_is_template_token`. Two rounds cover the doubly-escaped form
#: (&amp;lt; -> &lt; -> <); one spare round for deeper accidental escaping.
_MAX_UNESCAPE_ROUNDS = 3


def _is_template_token(raw: str) -> bool:
    """True when *raw* is an unsubstituted template-style token.

    Matches :data:`_PLACEHOLDER_TOKEN_RE` against the value itself and against
    its iteratively HTML-unescaped forms, so escaped variants like
    ``&lt;username&gt;``, ``&amp;lt;username&amp;gt;`` and ``&#60;username&#62;``
    are detected as well - channel adapters and webhook payloads sometimes
    escape angle brackets once (or twice) before the value reaches the tool.
    """
    candidate = raw
    for _ in range(_MAX_UNESCAPE_ROUNDS):
        if _PLACEHOLDER_TOKEN_RE.match(candidate):
            return True
        unescaped = html.unescape(candidate)
        if unescaped == candidate:
            return False
        candidate = unescaped
    return bool(_PLACEHOLDER_TOKEN_RE.match(candidate))

# Lowercase literal strings an LLM commonly uses as stand-in values when it
# does not have the real username. None of these are plausible production
# GitHub account names, so we reject them at the validation layer and surface
# a self-correcting error instead of wasting an API round-trip.
_PLACEHOLDER_LOGINS: frozenset[str] = frozenset(
    {
        "placeholder",
        "username",
        "collaborator",
        "contributor",
        "user",
        "youruser",
        "yourusername",
        "gitusername",
        "githubuser",
        "github-user",
        "example",
        "exampleuser",
        "sampleuser",
        "testuser",
        "actualusername",
        "realusername",
        "insertusername",
    }
)


def _coerce_str(value: Any) -> str:
    """Best-effort string coercion - ``""`` for non-strings.

    The integration tool-test endpoint dispatches straight to the handler and
    bypasses the agent loop's jsonschema coercion, so a schema string can arrive
    as a number/object. Returning ``""`` lets the required-field guards reply
    with a friendly error instead of ``.strip()`` raising ``AttributeError``.
    """
    return value if isinstance(value, str) else ""


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion; ``None`` when the value is not int-like.

    Rejects ``bool`` (a subclass of ``int``) so a malformed ``{"number": true}``
    is treated as invalid rather than silently acting on issue/PR 1.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_repo(repo: Any) -> tuple[str, str, str | None]:
    """Validate and split an ``owner/name`` repository identifier.

    Returns ``(owner, name, None)`` on success or ``("", "", error_string)``
    for anything that is not a single well-formed ``owner/name`` pair - the
    value is interpolated into request paths, so rejecting stray ``/`` / ``?`` /
    ``#`` / whitespace here prevents path- and query-injection into the API URL.
    """
    raw = _coerce_str(repo).strip()
    if not raw:
        return "", "", "Error: 'repo' is required in 'owner/name' format (e.g. 'octocat/Hello-World')."
    parts = raw.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return "", "", f"Error: invalid repository '{raw}'. Use the 'owner/name' format (e.g. 'octocat/Hello-World')."
    owner, name = parts[0], parts[1]
    if not _OWNER_RE.match(owner) or not _NAME_RE.match(name):
        return "", "", (
            f"Error: invalid repository '{raw}'. The owner may contain letters, "
            "digits and hyphens; the name may also contain '.' and '_'."
        )
    return owner, name, None


def repo_error(repo: Any) -> str | None:
    """Return a friendly error string when *repo* is not a valid ``owner/name``.

    Convenience wrapper for tools that only need the original ``owner/name``
    string (not the split parts) but still want path-injection-safe validation.
    """
    _, _, err = parse_repo(repo)
    return err


def _placeholder_hint(field: str) -> str:
    """Actionable guidance appended to placeholder rejections, per field kind.

    The PR-author hint (``user.login`` in the trigger payload) only makes
    sense when the rejected value is a person's login; for org / team /
    transfer-target fields it would send the agent to the wrong place.
    """
    if field in ("login", "username"):
        return (
            "Provide the actual GitHub username — for a pull request, "
            "extract it from the trigger payload's 'user.login' field."
        )
    return (
        f"Provide the actual GitHub {field} — extract it from the trigger "
        "payload or the task instructions instead of a stand-in value."
    )


def login_error(value: Any, field: str = "login") -> str | None:
    """Return a friendly error string when *value* is not a valid GitHub login.

    Used for org / user / team-member identifiers that are interpolated into
    request paths. Returns ``None`` when the value is a safe account login.

    Rejects literal stand-in logins (e.g. ``placeholder``) and unsubstituted
    template tokens (e.g. ``{{username}}``, ``<username>``) that an LLM may
    emit when it does not know the real account name, so the call fails
    immediately with a clear self-correcting error instead of hitting the
    GitHub API with a bogus name.
    """
    raw = _coerce_str(value).strip()
    if not raw:
        return f"Error: '{field}' is required."
    # Detect unsubstituted template-style tokens ({{...}}, <...>, [...]),
    # including HTML-escaped variants (&lt;...&gt;, &amp;lt;...&amp;gt;).
    if _is_template_token(raw):
        return (
            f"Error: '{field}' value {raw!r} looks like an unsubstituted template "
            f"placeholder. {_placeholder_hint(field)}"
        )
    # Detect literal stand-in strings the LLM uses when it does not know the
    # real account name.
    if raw.lower() in _PLACEHOLDER_LOGINS:
        return (
            f"Error: '{field}' value {raw!r} is a known placeholder, not a real "
            f"GitHub login. {_placeholder_hint(field)}"
        )
    if not _OWNER_RE.match(raw):
        return f"Error: invalid {field} '{raw}'. Logins may only contain letters, digits and hyphens."
    return None


#: The prefix GitHub gives an enterprise team's slug in an organization's team
#: listing (``ent:<slug>``): the team endpoints take it as it is (#2155).
ENTERPRISE_TEAM_PREFIX = "ent:"

#: A team slug: lower-case letters, digits, hyphens and underscores.
_TEAM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def team_slug_error(value: Any) -> str | None:
    """Why *value* is not a team slug ``list_github_teams`` returns, or ``None``.

    An enterprise team reads ``ent:<slug>`` in an organization's listing; the
    prefix is accepted, the rest is checked as a slug.
    """
    raw = _coerce_str(value).strip()
    if not raw:
        return "Error: 'team_slug' is required."
    if _is_template_token(raw):
        return (
            f"Error: 'team_slug' value {raw!r} looks like an unsubstituted template "
            f"placeholder. {_placeholder_hint('team_slug')}"
        )
    slug = raw[len(ENTERPRISE_TEAM_PREFIX) :] if raw.lower().startswith(ENTERPRISE_TEAM_PREFIX) else raw
    if not _TEAM_SLUG_RE.match(slug):
        return (
            f"Error: invalid team_slug '{raw}'. Pass the slug list_github_teams shows "
            "(letters, digits, hyphens, underscores; an enterprise team with its ent: prefix, e.g. ent:security-team)."
        )
    return None


def _enc(value: Any) -> str:
    """Percent-encode a free-form path segment (branch / ref / tag / file path).

    ``/`` is preserved so hierarchical refs (``feature/x``, ``heads/x``) and
    file paths (``docs/a b.md``) keep their separators, while characters that
    would otherwise break the path or inject a query/fragment (space, ``?``,
    ``#``, ``%`` ...) are encoded. ``owner`` / ``name`` / logins are validated
    separately by :func:`parse_repo` / :func:`login_error`; this guards the
    remaining free-form ref and path segments interpolated into request URLs.
    """
    return quote(_coerce_str(value), safe="/")


# ---------------------------------------------------------------------------
# Request guard: dot segments and the repository Scope(s)
# ---------------------------------------------------------------------------

#: The repository Scope(s) of the GitHub tool call in progress: lower-cased
#: ``owner/name`` names, or ``None`` outside a scoped call. Set by the guard
#: ``scope.apply_repo_scope`` wraps every scoped tool in, for the duration of
#: that one call, and read by :func:`safe_request` and by the listings whose
#: results span repositories.
_REPO_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("github_repo_scope", default=None)

# The repository a REST path addresses, read off the START of the path: a file
# path further down (``/contents/docs/repos/x/y``) is not a repository.
_REPO_PATHS = (
    re.compile(r"^/repos/([^/]+)/([^/]+)"),
    re.compile(r"^/orgs/[^/]+/teams/[^/]+/repos/([^/]+)/([^/]+)"),
    re.compile(r"^/teams/[^/]+/repos/([^/]+)/([^/]+)"),
)
_API_REPO_URL_RE = re.compile(r"/repos/([^/?#]+)/([^/?#]+)/?$")


def active_repo_scope() -> frozenset[str] | None:
    """The repository Scope(s) of the call in progress (``None`` = unscoped)."""
    return _REPO_SCOPE.get()


def in_repo_scope(full_name: Any) -> bool:
    """True when the call in progress may use repository *full_name*."""
    scope = _REPO_SCOPE.get()
    if scope is None:
        return True
    return isinstance(full_name, str) and full_name.strip().lower() in scope


def repo_from_api_url(url: Any) -> str:
    """``owner/name`` of a ``.../repos/{owner}/{name}`` API URL, else ``""``."""
    match = _API_REPO_URL_RE.search(url) if isinstance(url, str) else None
    return f"{unquote(match.group(1))}/{unquote(match.group(2))}" if match else ""


def request_path_error(url: str) -> str | None:
    """Why the GitHub request to *url* must not be sent, or ``None``.

    A ``..`` segment is never a real GitHub API path: httpx collapses it, so
    ``/repos/acme/api/contents/../../../other/x/contents/f`` would be sent as a
    read (or a write) on ``other/x``. The free-form path, branch and ref values
    interpolated by :func:`_enc` keep their ``/``, so this is the one place
    that can stop it. (A ``.`` segment is collapsed too, but cannot leave the
    path it sits in: ``./README.md`` keeps working.) Under a repository
    Scope(s) a path addressing a repository outside it is refused as well -
    whichever tool argument it came from.
    """
    segments = urlsplit(url).path.split("/")
    if any(unquote(segment) == ".." for segment in segments):
        return "Error: a path, branch or ref may not contain a '..' segment."
    scope = _REPO_SCOPE.get()
    if scope is None:
        return None
    path = "/".join(segment for segment in segments if unquote(segment) != ".")
    for pattern in _REPO_PATHS:
        match = pattern.match(path)
        if match is None:
            continue
        full_name = f"{unquote(match.group(1))}/{unquote(match.group(2))}"
        if full_name.lower() not in scope:
            allowed = ", ".join(sorted(scope)) or "none"
            return (
                f"Error: this request would reach repository '{full_name}', outside the "
                "Scope(s) set for this agent or assignment. Allowed repositories: "
                f"{allowed}. Work only on those - do not retry with another repository."
            )
        break
    return None


async def safe_request(
    client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
) -> tuple[Any, str | None]:
    """:func:`app.integrations.http.safe_request` behind :func:`request_path_error`."""
    if err := request_path_error(url):
        return None, err
    resp, err = await _http_safe_request(client, method, url, provider, action, **kwargs)
    if err and (moved := _MOVED_REPOSITORY_RE.search(err)):
        err = await _name_moved_repository(client, provider, err, moved.group(1), kwargs.get("headers"))
    return resp, err


#: A redirect GitHub answers for a transferred or renamed repository: the
#: request names the old ``owner/name``, the ``Location`` its ``/repositories/{id}``.
_MOVED_REPOSITORY_RE = re.compile(r"HTTP 30[1278]\b.*Moved to: https://api\.github\.com/repositories/(\d+)")


async def _name_moved_repository(
    client: Any, provider: str, err: str, repository_id: str, headers: dict[str, str] | None
) -> str:
    """*err* with the new ``owner/name`` of the moved repository, when it is readable (#2155).

    GitHub answers a request on a transferred or renamed repository's old name
    with a 301 to ``/repositories/{id}``; the caller needs the new name to call
    again, never the id. An unreadable repository keeps *err* as it is.
    """
    resp, read_err = await _http_safe_request(
        client,
        "get",
        f"{_BASE_URL}/repositories/{repository_id}",
        provider,
        f"read the moved repository {repository_id}",
        headers=headers or {},
    )
    full_name = ""
    if not read_err and resp is not None:
        try:
            full_name = str((resp.json() or {}).get("full_name") or "")
        except ValueError:
            full_name = ""
    if not full_name:
        return err
    return f"{err} The repository moved: it is now {full_name}. Call again with repo='{full_name}'."


# ---------------------------------------------------------------------------
# Permission / scope error hints
# ---------------------------------------------------------------------------

#: Lower-cased substrings that mark a GitHub permission / scope / membership
#: denial. GitHub answers governance reads (org members, teams, enterprise) with
#: a 403 "Resource not accessible by integration" (REST) or the equivalent
#: GraphQL message - it says access was denied but never *which* permission is
#: missing, so the governance tools detect these and append a concrete remedy.
_PERMISSION_SIGNATURES: tuple[str, ...] = (
    "not accessible by integration",
    "access denied",
    "insufficient permission",
    "insufficient scopes",
    "requires the following scope",
    "must have admin",
    "you must be",
    "do not have permission",
    "not have sufficient",
    "resource protected by organization",
    "forbidden",
    "(403)",
    "(401)",
)


def is_permission_error(err: str | None) -> bool:
    """True when *err* looks like a GitHub permission / scope / membership denial."""
    if not err:
        return False
    low = err.lower()
    return any(sig in low for sig in _PERMISSION_SIGNATURES)


def with_permission_hint(err: str, hint: str) -> str:
    """Append *hint* to *err* when it looks like a permission / scope denial.

    GitHub's "403 Resource not accessible by integration" (and its GraphQL
    equivalents) tells the agent *that* access was denied but not *which*
    permission to grant. Governance reads always map to a specific, documentable
    permission/scope, so surface it inline to make the failure actionable
    instead of leaving the agent (and auditor) guessing.
    """
    if err and is_permission_error(err):
        return f"{err}\nTo fix this, {hint}"
    return err


#: Remediation for Rulesets reads (repo + org). Shared by the ``rulesets`` tools
#: and the ruleset-aware branch-protection fallback in ``branches`` so a
#: permission-denied governance read always names the exact access to grant.
RULESET_PERMISSION_HINT = (
    "grant the GitHub App 'Repository -> Administration: Read' (organization rulesets "
    "need 'Organization -> Administration: Read'), or use a Personal Access Token with "
    "the 'repo' scope (and 'admin:org' for org rulesets)."
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _label_str(labels: list[dict] | None) -> str:
    names = [lb.get("name", "") for lb in (labels or []) if isinstance(lb, dict)]
    names = [n for n in names if n]
    return f" ({', '.join(names)})" if names else ""


def _body_preview(body: str | None, max_len: int = _MAX_BODY_PREVIEW) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    preview = text[:max_len] + ("…" if len(text) > max_len else "")
    return f"\n  Preview: {preview}"


def _truncate(text: str, limit: int, *, suffix: str = "\n... [truncated]") -> str:
    """Truncate *text* to *limit* chars, appending a short marker when cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + suffix


def one_line(text: str | None, limit: int = _ITEM_PREVIEW_CHARS) -> str:
    """*text* on one line, cut at *limit* characters with an ellipsis."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def window_args_error(offset: Any, max_chars: Any) -> tuple[int, int, str | None]:
    """``(offset, size, error)`` for a body window; the size defaults and is capped."""
    start = _coerce_int(offset)
    size = _coerce_int(max_chars)
    if start is None or start < 0:
        return 0, 0, "Error: 'offset' must be 0 or a positive integer."
    if size is None or size == 0:
        size = _DEFAULT_BODY_WINDOW
    if size < 1:
        return 0, 0, "Error: 'max_chars' must be a positive integer."
    return start, min(size, _MAX_BODY_WINDOW), None


def body_section(body: str | None, *, include: bool, offset: int, size: int, what: str) -> str:
    """The body of a PR or issue: its head by default, a window on request.

    By default the first :data:`_BODY_HEAD_CHARS` characters come, with the
    length and the argument that reads the rest. ``include`` reads
    ``[offset, offset + size)`` and names the next offset while more remains.
    """
    text = body or ""
    total = len(text)
    if not text.strip():
        return "\nBody: No description"
    if not include:
        if total <= _BODY_HEAD_CHARS:
            return f"\nBody:\n{text}"
        return (
            f"\nBody ({total:,} characters, the first {_BODY_HEAD_CHARS:,} shown; include_body=true reads it "
            f"by window with offset and max_chars):\n{text[:_BODY_HEAD_CHARS]}\n[… {total - _BODY_HEAD_CHARS:,} "
            "more characters]"
        )
    if offset >= total:
        return f"\nBody: offset {offset:,} is past the end of the {what} body ({total:,} characters)."
    end = min(total, offset + size)
    more = f"; {total - end:,} more, continue with offset={end}" if end < total else "; the end"
    return f"\nBody (characters {offset:,} to {end:,} of {total:,}{more}):\n{text[offset:end]}"


def _login(obj: dict | None) -> str:
    """Safely extract a ``login`` from a user-ish object."""
    if isinstance(obj, dict):
        return str(obj.get("login") or "?")
    return "?"


def summarize_branch_rules(rules: Any) -> str | None:
    """Summarize a list of GitHub *rule* objects into human-readable lines.

    Shared by the Rulesets tools (``get_github_branch_rules`` /
    ``get_github_ruleset``) and the ruleset-aware branch-protection fallback.
    Accepts either the effective-rules shape returned by
    ``GET /repos/{o}/{r}/rules/branches/{branch}`` (each rule carries a
    ``ruleset_id`` / ``ruleset_source_type``) or the ``rules`` array inside a
    ruleset object (no source fields). Returns ``None`` when there are no rules.

    The summary is deliberately governance-focused (required reviews, status
    checks, signed commits, linear history, force-push / deletion blocks) so it
    is usable as change-control evidence (SOC 2 CC8.1, FedRAMP CM-3) for a
    branch governed by a Ruleset rather than classic branch protection.
    """
    if not isinstance(rules, list) or not rules:
        return None
    lines: list[str] = []
    sources: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rtype = rule.get("type")
        params = rule.get("parameters") or {}
        src_id = rule.get("ruleset_id")
        if src_id is not None:
            sources.add(f"#{src_id} ({rule.get('ruleset_source_type') or '?'})")
        if rtype == "pull_request":
            bits = [f"{params.get('required_approving_review_count', 0)} approving review(s)"]
            if params.get("dismiss_stale_reviews_on_push"):
                bits.append("dismiss stale approvals")
            if params.get("require_code_owner_review"):
                bits.append("code owner review")
            if params.get("require_last_push_approval"):
                bits.append("last-push approval")
            if params.get("required_review_thread_resolution"):
                bits.append("conversation resolution")
            lines.append("  - Pull request required: " + ", ".join(bits))
        elif rtype == "required_status_checks":
            checks = params.get("required_status_checks") or []
            contexts = [c.get("context") for c in checks if isinstance(c, dict) and c.get("context")]
            strict = params.get("strict_required_status_checks_policy")
            lines.append(
                f"  - Required status checks ({'strict' if strict else 'non-strict'}): "
                + (", ".join(contexts) if contexts else "none specified")
            )
        elif rtype == "required_signatures":
            lines.append("  - Signed commits required")
        elif rtype == "required_linear_history":
            lines.append("  - Linear history required")
        elif rtype == "non_fast_forward":
            lines.append("  - Force pushes blocked (non-fast-forward)")
        elif rtype == "deletion":
            lines.append("  - Branch deletion blocked")
        elif rtype == "creation":
            lines.append("  - Ref creation restricted")
        elif rtype == "update":
            lines.append("  - Ref updates restricted")
        elif rtype == "required_deployments":
            envs = params.get("required_deployment_environments") or []
            lines.append("  - Required deployments: " + (", ".join(envs) if envs else "configured"))
        elif rtype:
            # Surface any other rule type generically so nothing is silently dropped.
            lines.append(f"  - {str(rtype).replace('_', ' ')}")
    if not lines:
        return None
    if sources:
        lines.append("  Source ruleset(s): " + ", ".join(sorted(sources)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GraphQL execution
# ---------------------------------------------------------------------------


_MUTATION = re.compile(r"\bmutation\b")


def _is_mutation(query: str) -> bool:
    """Whether a GraphQL document may carry a mutation: the word anywhere, so the doubt is never retried."""
    return bool(_MUTATION.search(query or ""))


async def gh_graphql(
    client: Any,
    headers: dict[str, str],
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    action: str = "execute GraphQL query",
    extra_headers: dict[str, str] | None = None,
    keep_partial: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Execute a GitHub GraphQL request and return ``(data, error)``.

    Routes through :func:`safe_request` (so transport/HTTP errors are formatted
    for the LLM) and additionally surfaces top-level GraphQL ``errors`` as a
    human-readable error string - GitHub returns HTTP 200 with an ``errors``
    array for query/permission problems, so the HTTP status alone is not enough.
    Returns ``(data, None)`` on success, ``(None, error_string)`` otherwise.

    ``keep_partial``: on a failure, the ``data`` object the answer carries
    anyway (GraphQL partial data next to ``errors``, on a 200 or in the JSON
    body of a non-2xx) is returned with the error, ``(partial, error_string)``,
    so a caller can say which fields of a mutation were applied.
    """
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    hdr = _json_headers(headers)
    if extra_headers:
        hdr = {**hdr, **extra_headers}
    # A query is idempotent: a gateway 502/503/504 is retried with the shared
    # backoff. A mutation is never retried (it may have been applied).
    retry = None if _is_mutation(query) else TRANSIENT_RETRY_STATUSES
    extra: dict[str, Any] = {"expected_statuses": set()} if keep_partial else {}
    resp, err = await safe_request(
        client, "post", _GRAPHQL_URL, _PROVIDER, action, headers=hdr, json=payload, retry_statuses=retry, **extra
    )
    if err:
        return (_partial_data(resp) if keep_partial else None), err
    # GitHub can answer a 200 with a JSON `null`, an empty body, or a non-JSON
    # body (transient 5xx/502, secondary rate limiting, abuse-detection). Guard
    # against that so a malformed response surfaces a clear, retryable error
    # instead of crashing with an opaque "'NoneType' object has no attribute 'get'".
    try:
        body = resp.json() if resp is not None else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return None, (
            f"Error: GitHub returned an unexpected, empty, or non-JSON response while trying to {action}. "
            "This usually means a transient 5xx/502, secondary rate limiting, or an "
            "abuse-detection response - retry shortly."
        )
    gql_errors = body.get("errors")
    if gql_errors:
        msgs = [e.get("message", "Unknown error") for e in gql_errors if isinstance(e, dict)]
        partial = body.get("data") if keep_partial and isinstance(body.get("data"), dict) else None
        return partial, "Error: GitHub GraphQL error: " + "; ".join(msgs or ["Unknown error"])
    data = body.get("data")
    if data is None:
        return None, "Error: GitHub GraphQL response contained no data."
    return data, None


def _partial_data(resp: Any) -> dict[str, Any] | None:
    """The ``data`` object of a failed GraphQL answer's JSON body, when it has one."""
    if resp is None:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else None
