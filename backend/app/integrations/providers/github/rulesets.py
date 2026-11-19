"""GitHub Rulesets tools - the modern branch / tag / push governance surface.

Rulesets are GitHub's replacement for classic branch protection. They can be
defined at the **repository** OR **organization** level, target branches / tags
/ pushes, and carry rules (required pull-request reviews, required status
checks, signed commits, linear history, force-push and deletion blocks, ...).

Crucially, a branch governed by a ruleset is reported as *"not protected"* by
the classic branch-protection API - so an auditor checking change-control only
via ``get_github_branch_protection`` sees a false negative. These tools expose
the ruleset surface (and the *effective* rules applying to a branch) so the
change-control proof is airtight for ruleset-governed default branches like
``main`` / ``master`` (SOC 2 CC8.1, FedRAMP CM-3).

Docs:
  - Repo rulesets:       https://docs.github.com/en/rest/repos/rules
  - Org rulesets:        https://docs.github.com/en/rest/orgs/rules
  - Rules for a branch:  GET /repos/{owner}/{repo}/rules/branches/{branch}
"""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _BASE_URL,
    _PROVIDER,
    RULESET_PERMISSION_HINT,
    _coerce_int,
    _coerce_str,
    _enc,
    login_error,
    parse_repo,
)

_SOURCE = "integration:github"

#: Concrete remediation appended (only on permission errors) to ruleset reads.
#: Single source of truth lives in ``_common`` so the branch-protection fallback
#: (branches.py) and these tools never drift.
_RULESET_HINT = RULESET_PERMISSION_HINT

#: Said when a ruleset id cannot be read and may be an ENTERPRISE ruleset: an
#: enterprise ruleset is neither on the repository nor on the organization
#: surface, so both answer 'Not Found' or a misleading org-permission error.
_ENTERPRISE_RULESET_NOTE = (
    "Ruleset #{rid} is an enterprise ruleset ({source}): its own configuration is readable by an "
    "enterprise owner only, never through the repository or organization endpoints. The rules it "
    "enforces on a branch are listed by get_github_branch_rules(repo, branch)."
)
_MAYBE_ENTERPRISE_NOTE = (
    "If the id came from get_github_branch_rules or list_github_rulesets with source Enterprise, it "
    "is an enterprise ruleset: neither the repository nor the organization endpoint serves it; "
    "get_github_branch_rules(repo, branch) lists the rules it enforces."
)


def _enterprise_source(listing: Any, rid: int) -> str | None:
    """The enterprise name of ruleset *rid* in a repository listing, None when it is not one."""
    if not isinstance(listing, list):
        return None
    for r in listing:
        if isinstance(r, dict) and r.get("id") == rid and str(r.get("source_type") or "").lower() == "enterprise":
            return str(r.get("source") or "enterprise")
    return None


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub Rulesets tools and register their callables."""

    async def get_github_branch_rules(repo: str, branch: str) -> str:
        """List the rules that effectively apply to a branch (from ALL rulesets).

        This is the airtight change-control proof for a branch: it returns the
        merged set of active rules (required reviews, status checks, etc.) that
        apply to ``branch`` from every repository AND organization ruleset,
        regardless of classic branch protection.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if not _coerce_str(branch).strip():
            return "Error: 'branch' is required."
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/rules/branches/{_enc(branch)}",
                _PROVIDER,
                f"get effective rules for {branch} in {repo}",
                headers=headers,
            )
            if err:
                return _common.with_permission_hint(err, _RULESET_HINT)
            summary = _common.summarize_branch_rules(resp.json())
            if not summary:
                return (
                    f"No active ruleset rules apply to branch '{branch}' in {repo}. "
                    "It may instead use classic branch protection - check "
                    "get_github_branch_protection."
                )
            return f"Active ruleset rules for branch '{branch}' in {repo}:\n{summary}"

    async def list_github_rulesets(
        repo: str = "", org: str = "", count: int = 30, includes_parents: bool = True
    ) -> str:
        """List rulesets for a repository (repo='owner/name') OR an organization (org=login).

        For a repository, ``includes_parents`` also returns the organization
        rulesets that apply to it (default true) - important because a default
        branch is often governed by an ORG-level ruleset, not a repo one.
        """
        n = max(1, min(count, 100))
        if _coerce_str(repo).strip() and _coerce_str(org).strip():
            return "Error: provide either 'repo' or 'org', not both - rulesets are scoped to one surface."
        async with egress_client(timeout=30) as client:
            if _coerce_str(repo).strip():
                owner, name, rerr = parse_repo(repo)
                if rerr:
                    return rerr
                url = f"{_BASE_URL}/repos/{owner}/{name}/rulesets"
                params: dict[str, Any] = {
                    "per_page": n,
                    "includes_parents": "true" if includes_parents else "false",
                }
                scope = repo
            elif _coerce_str(org).strip():
                if e := login_error(org, "org"):
                    return e
                url = f"{_BASE_URL}/orgs/{org}/rulesets"
                params = {"per_page": n}
                scope = f"org {org}"
            else:
                return "Error: provide either 'repo' (owner/name) or 'org' (organization login)."
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"list rulesets for {scope}", headers=headers, params=params
            )
            if err:
                return _common.with_permission_hint(err, _RULESET_HINT)

            def _fmt(r: dict) -> str:
                src_type = r.get("source_type") or "?"
                src_name = r.get("source")
                src = f"{src_type}: {src_name}" if src_name else src_type
                return (
                    f"#{r.get('id')} {r.get('name')} [target={r.get('target', 'branch')}] "
                    f"enforcement={r.get('enforcement', '?')} ({src})"
                )

            return _common.format_entity_list(
                resp.json(), _fmt, noun="ruleset", empty_msg=f"No rulesets found for {scope}."
            )

    async def get_github_ruleset(ruleset_id: int, repo: str = "", org: str = "") -> str:
        """Get a ruleset's full configuration: enforcement, target refs, bypass actors, and rules.

        Identify the ruleset by its id (from list_github_rulesets) plus the
        scope it lives in: repo='owner/name' for a repository ruleset, or
        org=login for an organization ruleset.
        """
        rid = _coerce_int(ruleset_id)
        if rid is None:
            return "Error: 'ruleset_id' must be an integer (the id from list_github_rulesets)."
        if _coerce_str(repo).strip() and _coerce_str(org).strip():
            return "Error: provide either 'repo' or 'org', not both - a ruleset id lives on exactly one surface."
        async with egress_client(timeout=30) as client:
            if _coerce_str(repo).strip():
                owner, name, rerr = parse_repo(repo)
                if rerr:
                    return rerr
                url = f"{_BASE_URL}/repos/{owner}/{name}/rulesets/{rid}"
                params: dict[str, Any] = {"includes_parents": "true"}
                scope = repo
            elif _coerce_str(org).strip():
                if e := login_error(org, "org"):
                    return e
                url = f"{_BASE_URL}/orgs/{org}/rulesets/{rid}"
                params = {}
                scope = f"org {org}"
            else:
                return "Error: provide either 'repo' (owner/name) or 'org' (organization login)."
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"get ruleset #{rid} for {scope}", headers=headers, params=params
            )
            if err:
                if _coerce_str(repo).strip():
                    # A repository listing with its parents names an enterprise
                    # ruleset's source, so say exactly why it is unreadable.
                    listing, lerr = await _common.safe_request(
                        client,
                        "get",
                        f"{_BASE_URL}/repos/{owner}/{name}/rulesets",
                        _PROVIDER,
                        f"list rulesets for {repo}",
                        headers=headers,
                        params={"per_page": 100, "includes_parents": "true"},
                    )
                    source = None if lerr or listing is None else _enterprise_source(listing.json(), rid)
                    if source:
                        return _ENTERPRISE_RULESET_NOTE.format(rid=rid, source=f"Enterprise: {source}")
                    return _common.with_permission_hint(err, _RULESET_HINT)
                return f"{_common.with_permission_hint(err, _RULESET_HINT)}\n{_MAYBE_ENTERPRISE_NOTE}"
            rs = resp.json()
            header = (
                f"Target: {rs.get('target', 'branch')} | Enforcement: {rs.get('enforcement', '?')} | "
                f"Source: {rs.get('source_type', '?')} {rs.get('source', '')}"
            ).rstrip()
            lines = [f"Ruleset #{rs.get('id')}: {rs.get('name')} ({scope})", header]
            ref_cond = ((rs.get("conditions") or {}).get("ref_name")) or {}
            inc = ref_cond.get("include") or []
            exc = ref_cond.get("exclude") or []
            if inc or exc:
                parts = []
                if inc:
                    parts.append("include " + ", ".join(inc))
                if exc:
                    parts.append("exclude " + ", ".join(exc))
                lines.append("Applies to refs: " + " | ".join(parts))
            actors = rs.get("bypass_actors") or []
            if actors:
                actor_strs = []
                for a in actors:
                    if not isinstance(a, dict):
                        continue
                    aid = a.get("actor_id")
                    actor_strs.append(
                        f"{a.get('actor_type', '?')}"
                        f"{f'#{aid}' if aid is not None else ''} ({a.get('bypass_mode', '?')})"
                    )
                if actor_strs:
                    lines.append("Bypass actors: " + ", ".join(actor_strs))
            summary = _common.summarize_branch_rules(rs.get("rules") or [])
            lines.append("Rules:\n" + summary if summary else "Rules: none")
            return "\n".join(lines)

    executor.register("get_github_branch_rules", get_github_branch_rules)
    executor.register("list_github_rulesets", list_github_rulesets)
    executor.register("get_github_ruleset", get_github_ruleset)

    return [
        ToolDefinition(
            name="get_github_branch_rules",
            description=(
                "List the rules that EFFECTIVELY apply to a branch from all repository AND organization "
                "rulesets (required reviews, status checks, signed commits, linear history, force-push/deletion "
                "blocks). Use this for change-control evidence on a branch governed by a Ruleset - classic "
                "branch protection (get_github_branch_protection) reports such a branch as 'not protected'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "Branch name (e.g. 'main')"},
                },
                "required": ["repo", "branch"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_rulesets",
            description=(
                "List rulesets governing a repository (repo='owner/name') or an organization (org=login). "
                "Returns each ruleset's id, name, target (branch/tag/push), enforcement (active/evaluate/disabled), "
                "and source. For a repo, includes_parents (default true) also lists the org rulesets that apply to it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository owner/name (omit if using org)", "default": ""},
                    "org": {"type": "string", "description": "Organization login (omit if using repo)", "default": ""},
                    "count": {"type": "integer", "description": "Max rulesets (max 100)", "default": 30},
                    "includes_parents": {
                        "type": "boolean",
                        "description": "For a repo, also include org rulesets that apply to it",
                        "default": True,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_ruleset",
            description=(
                "Get a ruleset's full configuration: enforcement, target refs (include/exclude patterns), "
                "bypass actors, and the complete rule set. Identify it by ruleset_id (from list_github_rulesets) "
                "plus repo='owner/name' for a repository ruleset or org=login for an organization ruleset. "
                "An enterprise ruleset (source Enterprise) is named as such: get_github_branch_rules lists its rules."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ruleset_id": {"type": "integer", "description": "Ruleset id from list_github_rulesets"},
                    "repo": {"type": "string", "description": "Repository owner/name (for a repo ruleset)", "default": ""},
                    "org": {"type": "string", "description": "Organization login (for an org ruleset)", "default": ""},
                },
                "required": ["ruleset_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
