"""GitHub branches, tags & git refs tools - list/create branches, tags, protection, ref management."""

from __future__ import annotations

import re
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, RULESET_PERMISSION_HINT, _enc, _json_headers, parse_repo
from .landed import branch_key, branch_probe

_SOURCE = "integration:github"

_SHA_RE = re.compile(r"[0-9a-fA-F]{4,40}")
#: GitHub's 422 on create ref when the ref is already there ("Reference already exists").
_REF_EXISTS_RE = re.compile(r"reference already exists", re.IGNORECASE)


def _looks_like_sha(value: str) -> bool:
    """Whether *value* reads as a full or short commit SHA (4 to 40 hexadecimal characters)."""
    return bool(_SHA_RE.fullmatch(value or ""))


#: GitHub's 422 on a ref write a ruleset refused: it never names the ruleset.
_REF_REFUSED_RE = re.compile(r"reference update failed|repository rule violations", re.IGNORECASE)


def rulesets_line(rulesets: Any, target: str) -> str:
    """The active rulesets of *target* (``branch`` / ``tag``) as one sentence, '' when none. Pure."""
    if not isinstance(rulesets, list):
        return ""
    named = [
        f"'{r.get('name') or '?'}' (id {r.get('id')}, {str(r.get('source_type') or 'Repository').lower()})"
        for r in rulesets
        if isinstance(r, dict)
        and str(r.get("target") or "").lower() == target
        and str(r.get("enforcement") or "").lower() == "active"
    ]
    if not named:
        return ""
    return (
        f" Active {target} ruleset(s) of the repository: {', '.join(named)}; "
        "read one with get_github_ruleset to see the rule and its bypass actors."
    )


async def with_ruleset_names(client: Any, headers: dict[str, str], base: str, err: str, target: str) -> str:
    """*err* naming the rulesets that may have refused a ref write (#2155).

    GitHub answers a ref write a ruleset refuses with a bare 422 "Reference
    update failed". The active rulesets targeting that kind of ref are read once
    and named; any other error, or an unreadable list, is answered as it is.
    """
    if not _REF_REFUSED_RE.search(err or ""):
        return err
    resp, read_err = await _common.safe_request(
        client,
        "get",
        f"{base}/rulesets",
        _PROVIDER,
        "list the rulesets behind the refusal",
        headers=headers,
        params={"includes_parents": "true", "per_page": 100},
    )
    if read_err or resp is None:
        return f"{err} A ruleset may have refused it; the rulesets could not be read: {read_err}"
    try:
        line = rulesets_line(resp.json(), target)
    except ValueError:
        line = ""
    return f"{err}{line}" if line else f"{err} No active {target} ruleset is visible to this token."


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub branch / tag / ref tools and register their callables."""

    async def list_github_branches(repo: str, count: int = 30) -> str:
        """List branches of a repository with protection status."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/branches",
                _PROVIDER,
                f"list branches in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            branches = resp.json()
            if not branches:
                return f"No branches found in {repo}."
            lines = []
            for b in branches:
                protected = " [protected]" if b.get("protected") else ""
                lines.append(f"{b['name']}{protected}")
            return f"Branches in {repo} ({len(branches)}):\n" + "\n".join(lines)

    async def list_github_tags(repo: str, count: int = 30) -> str:
        """List a repository's tags (most recent first) with their commit SHA."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/tags",
                _PROVIDER,
                f"list tags in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            tags = resp.json()
            return _common.format_entity_list(
                tags,
                lambda t: f"{t.get('name')} → {(t.get('commit') or {}).get('sha', '?')[:8]}",
                noun="tag",
                empty_msg=f"No tags found in {repo}.",
            )

    async def get_github_branch_protection(repo: str, branch: str) -> str:
        """Read a branch's protection rules (required reviews, status checks, enforcement)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/branches/{_enc(branch)}/protection",
                _PROVIDER,
                f"get branch protection for {branch} in {repo}",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                # Classic branch protection is absent, but the branch may be
                # governed by a repository/organization Ruleset (the modern
                # replacement) - which the protection API never reports. Check
                # the effective rules so we don't wrongly declare it unprotected.
                rules_resp, rules_err = await _common.safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/repos/{owner}/{name}/rules/branches/{_enc(branch)}",
                    _PROVIDER,
                    f"get effective rules for {branch} in {repo}",
                    headers=headers,
                )
                if rules_err:
                    # The ruleset check itself failed - we genuinely don't know
                    # whether a ruleset governs the branch, so DON'T claim it is
                    # unprotected. Surface why, and (for a permission error) name
                    # the exact access needed to verify ruleset governance.
                    return _common.with_permission_hint(
                        f"Branch '{branch}' in {repo} has no classic branch-protection rule, "
                        f"and its ruleset governance could not be verified: {rules_err}",
                        RULESET_PERMISSION_HINT,
                    )
                summary = (
                    _common.summarize_branch_rules(rules_resp.json()) if rules_resp is not None else None
                )
                if summary:
                    return (
                        f"Branch '{branch}' in {repo} has no classic branch-protection rule, "
                        f"but is governed by a Ruleset:\n{summary}\n"
                        "(Use get_github_ruleset for the full ruleset config.)"
                    )
                return (
                    f"Branch '{branch}' in {repo} is not protected: no classic branch-protection "
                    "rule and no active ruleset applies. Use list_github_rulesets / "
                    "get_github_branch_rules to double-check repo and org rulesets."
                )
            if err:
                return err
            p = resp.json()
            lines = [f"Branch protection for {branch} in {repo}:"]
            reviews = p.get("required_pull_request_reviews")
            if reviews:
                lines.append(
                    f"  Required approving reviews: {reviews.get('required_approving_review_count', 0)} | "
                    f"Dismiss stale: {reviews.get('dismiss_stale_reviews', False)} | "
                    f"Code owner reviews: {reviews.get('require_code_owner_reviews', False)}"
                )
            checks = p.get("required_status_checks")
            if checks:
                contexts = checks.get("contexts") or [c.get("context") for c in (checks.get("checks") or [])]
                lines.append(f"  Required status checks (strict={checks.get('strict', False)}): {', '.join(c for c in contexts if c) or 'none'}")
            lines.append(f"  Enforce admins: {(p.get('enforce_admins') or {}).get('enabled', False)}")
            lines.append(f"  Require linear history: {(p.get('required_linear_history') or {}).get('enabled', False)}")
            lines.append(f"  Allow force pushes: {(p.get('allow_force_pushes') or {}).get('enabled', False)}")
            lines.append(f"  Require conversation resolution: {(p.get('required_conversation_resolution') or {}).get('enabled', False)}")
            return "\n".join(lines)

    async def _default_branch(client: Any, base: str, repo: str) -> tuple[str, str | None]:
        """The repository's default branch read from its metadata, or the error of that read."""
        resp, err = await _common.safe_request(
            client, "get", base, _PROVIDER, f"get default branch for {repo}", headers=headers
        )
        if err or resp is None:
            return "", err or f"Could not read the default branch of {repo}."
        return str((resp.json() or {}).get("default_branch") or "").strip(), None

    async def _existing_branch_answer(client: Any, base: str, repo: str, branch: str, sha: str, err: str) -> str:
        """The answer to a creation GitHub refused because *branch* exists: success when it is at *sha*.

        A repeat (a resumed turn, a retry) of a creation that landed finds the
        branch where it would have put it; a branch elsewhere stays refused, both
        SHAs named. *err* is answered when the branch cannot be read.
        """
        resp, read_err = await _common.safe_request(
            client,
            "get",
            f"{base}/git/ref/heads/{_enc(branch)}",
            _PROVIDER,
            f"read branch {branch} in {repo}",
            headers=headers,
        )
        if read_err or resp is None:
            return err
        current = str(((resp.json() or {}).get("object") or {}).get("sha") or "")
        if current == sha:
            return f"Branch '{branch}' already exists in {repo} at {sha[:8]}, nothing created (ref refs/heads/{branch})"
        return (
            f"Error: branch '{branch}' already exists in {repo} at {current[:8] or 'an unreadable SHA'}, "
            f"not at {sha[:8]}. Nothing created: pick another name, or work on the existing branch."
        )

    async def create_github_branch(repo: str, branch: str, from_branch: str = "") -> str:
        """Create a new branch from a base branch, or at a commit SHA.

        Without *from_branch* the base is the repository's default branch, read
        from the repository (never a hard-coded ``main``: a repository whose
        default is ``master`` may keep a stale ``main``). A named *from_branch*
        is looked up as a branch first; when no branch carries that name and
        the value reads as a commit SHA (4 to 40 hexadecimal characters), the
        commit is resolved instead (a short SHA must be unambiguous) and the
        branch is created at it. The answer names the base branch and its SHA,
        and flags a named base that is not the default branch.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        base = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=30) as client:
            default, default_err = await _default_branch(client, base, repo)
            from_branch = (from_branch or "").strip()
            if not from_branch:
                if default_err:
                    return f"{default_err}\nNothing created: pass from_branch to name the base branch."
                if not default:
                    return (
                        f"{repo} reports no default branch. Nothing created: pass from_branch to name the base branch."
                    )
                from_branch = default
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base}/git/ref/heads/{_enc(from_branch)}",
                _PROVIDER,
                f"get ref for {from_branch} in {repo}",
                headers=headers,
            )
            at_commit = False
            if err:
                not_found = "not found" in err.lower() or "No commit found" in err
                if not_found and _looks_like_sha(from_branch):
                    sha, commit_err = await _resolve_commit(client, base, repo, from_branch)
                    if commit_err:
                        return commit_err
                    at_commit = True
                else:
                    if not_found and default and default != from_branch:
                        return (
                            f"Branch '{from_branch}' not found in {repo}. "
                            f"The default branch is '{default}'. Retry with from_branch='{default}'."
                        )
                    return err
            else:
                sha = resp.json()["object"]["sha"]
            resp, err = await _common.safe_request(
                client,
                "post",
                f"{base}/git/refs",
                _PROVIDER,
                f"create branch {branch} in {repo}",
                headers=_json_headers(headers),
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            if err:
                if not _REF_EXISTS_RE.search(err):
                    return err
                return await _existing_branch_answer(client, base, repo, branch, sha, err)
            if at_commit:
                return f"Branch '{branch}' created in {repo} at commit {sha} (ref refs/heads/{branch})"
            created = f"Branch '{branch}' created in {repo} from {from_branch} (SHA: {sha[:8]})"
            if not default:
                return f"{created}. The default branch of {repo} could not be read."
            if from_branch == default:
                return f"{created}, the default branch."
            return (
                f"{created}. Warning: '{from_branch}' is not the default branch of {repo} "
                f"('{default}'); omit from_branch to start from the default branch."
            )

    async def _resolve_commit(client: Any, base: str, repo: str, ref: str) -> tuple[str, str | None]:
        """The full SHA of commit *ref* (a full or unambiguous short SHA), or the one-line refusal."""
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{base}/commits/{_enc(ref)}",
            _PROVIDER,
            f"get commit {ref} in {repo}",
            headers=headers,
            expected_statuses={404, 409, 422},
        )
        if err or resp is None or resp.status_code >= 400:
            return "", f"Branch or commit not found: {ref} (in {repo}; nothing created)."
        sha = str((resp.json() or {}).get("sha") or "")
        if not sha:
            return "", f"Branch or commit not found: {ref} (in {repo}; nothing created)."
        return sha, None

    async def _compare(client: Any, base: str, older: str, newer: str) -> tuple[dict[str, Any] | None, str | None]:
        """``compare older...newer`` (status, ahead_by, behind_by) or the error."""
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{base}/compare/{_enc(older)}...{_enc(newer)}",
            _PROVIDER,
            f"compare {older[:12]}...{newer[:12]}",
            headers=headers,
            params={"per_page": "1"},
        )
        if err:
            return None, err
        return resp.json(), None

    async def _update_branch(client: Any, repo: str, owner: str, base: str, branch: str, sha: str, force: bool) -> str:
        """Move ``branch`` to ``sha``: fast-forward only unless ``force``, never onto an open PR's base."""
        branch = branch.strip()
        target = sha.strip()
        if not branch:
            return "Error: 'branch' is required for update_branch."
        if not target:
            return "Error: 'sha' is required for update_branch (the commit the branch moves to)."
        resp, err = await _common.safe_request(
            client, "get", f"{base}/git/ref/heads/{_enc(branch)}", _PROVIDER, f"resolve {branch}", headers=headers
        )
        if err:
            return err
        tip = resp.json()["object"]["sha"]
        if tip == target:
            return f"Branch '{branch}' of {repo} is already at {target[:8]}: nothing moved."

        # An open pull request whose head would equal its base is closed by GitHub and
        # cannot be reopened from that state: refused whatever ``force`` says.
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{base}/pulls",
            _PROVIDER,
            f"list open pull requests of {branch}",
            headers=headers,
            params={"state": "open", "head": f"{owner}:{branch}", "per_page": "30"},
        )
        if err:
            return err
        for pull in resp.json() or []:
            base_ref = ((pull.get("base") or {}).get("ref")) or ""
            if not base_ref:
                continue
            compared, err = await _compare(client, base, base_ref, target)
            if err:
                return err
            if int((compared or {}).get("ahead_by") or 0) == 0:
                return (
                    f"Error: moving '{branch}' to {target[:8]} would leave pull request #{pull.get('number')} "
                    f"('{pull.get('title', '')}') with nothing ahead of its base '{base_ref}': GitHub closes the "
                    "pull request by itself and it cannot be reopened. Nothing moved. To bring the base into the "
                    "branch use manage_github_pr action=update_branch; to change the branch, add a commit on it "
                    "(create_github_commit)."
                )

        if not force:
            compared, err = await _compare(client, base, tip, target)
            if err:
                return err
            status = (compared or {}).get("status", "")
            if status not in ("ahead", "identical"):
                return (
                    f"Error: {target[:8]} does not contain the tip {tip[:8]} of '{branch}' (compare status "
                    f"'{status}'): the move is not a fast-forward and would drop "
                    f"{int((compared or {}).get('behind_by') or 0)} commit(s). Nothing moved. Pass force=true "
                    "only when dropping them is intended."
                )
        resp, err = await _common.safe_request(
            client,
            "patch",
            f"{base}/git/refs/heads/{_enc(branch)}",
            _PROVIDER,
            f"move branch {branch}",
            headers=_json_headers(headers),
            json={"sha": target, "force": bool(force)},
        )
        if err:
            return await with_ruleset_names(client, headers, base, err, "branch")
        kind = "forced" if force else "fast-forward"
        return f"Branch '{branch}' of {repo} moved from {tip[:8]} to {target[:8]} ({kind})."

    async def manage_github_ref(
        repo: str,
        action: str,
        tag: str = "",
        branch: str = "",
        sha: str = "",
        from_ref: str = "",
        force: bool = False,
    ) -> str:
        """Manage git refs (write / destructive).

        ``action`` is one of:
          - ``create_tag`` — create a lightweight tag ``tag`` pointing at ``sha``
            (or the head of ``from_ref`` when ``sha`` is omitted).
          - ``update_branch`` — move ``branch`` to ``sha``: a fast-forward unless
            ``force``; never a move that leaves an open pull request with nothing
            ahead of its base (GitHub would close it).
          - ``delete_branch`` — delete ``branch``. Cannot be undone.
          - ``delete_tag`` — delete ``tag``. Cannot be undone.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        base = f"{_BASE_URL}/repos/{owner}/{name}"
        async with egress_client(timeout=30) as client:
            if action == "create_tag":
                if not tag.strip():
                    return "Error: 'tag' is required for create_tag."
                target = sha.strip()
                if not target:
                    ref_name = from_ref.strip()
                    if not ref_name:
                        ref_name, default_err = await _default_branch(client, base, repo)
                        if default_err:
                            return default_err
                        if not ref_name:
                            return f"{repo} reports no default branch: pass sha or from_ref for create_tag."
                    ref_path = ref_name if ref_name.startswith(("heads/", "tags/")) else f"heads/{ref_name}"
                    resp, err = await _common.safe_request(
                        client, "get", f"{base}/git/ref/{_enc(ref_path)}", _PROVIDER, f"resolve {ref_name}", headers=headers
                    )
                    if err:
                        return err
                    target = resp.json()["object"]["sha"]
                resp, err = await _common.safe_request(
                    client,
                    "post",
                    f"{base}/git/refs",
                    _PROVIDER,
                    f"create tag {tag} in {repo}",
                    headers=_json_headers(headers),
                    json={"ref": f"refs/tags/{tag}", "sha": target},
                )
                if err:
                    return await with_ruleset_names(client, headers, base, err, "tag")
                return f"Tag '{tag}' created in {repo} at {target[:8]}."
            if action == "update_branch":
                return await _update_branch(client, repo, owner, base, branch, sha, force)
            if action == "delete_branch":
                if not branch.strip():
                    return "Error: 'branch' is required for delete_branch."
                resp, err = await _common.safe_request(
                    client, "delete", f"{base}/git/refs/heads/{_enc(branch)}", _PROVIDER, f"delete branch {branch}", headers=headers
                )
                if err:
                    return await with_ruleset_names(client, headers, base, err, "branch")
                return f"Branch '{branch}' deleted from {repo}."
            if action == "delete_tag":
                if not tag.strip():
                    return "Error: 'tag' is required for delete_tag."
                resp, err = await _common.safe_request(
                    client, "delete", f"{base}/git/refs/tags/{_enc(tag)}", _PROVIDER, f"delete tag {tag}", headers=headers
                )
                if err:
                    return await with_ruleset_names(client, headers, base, err, "tag")
                return f"Tag '{tag}' deleted from {repo}."
            return "Error: invalid action. Use create_tag, update_branch, delete_branch, or delete_tag."

    executor.register("list_github_branches", list_github_branches)
    executor.register("list_github_tags", list_github_tags)
    executor.register("get_github_branch_protection", get_github_branch_protection)
    executor.register("create_github_branch", create_github_branch)
    executor.register("manage_github_ref", manage_github_ref)

    return [
        ToolDefinition(
            name="list_github_branches",
            description="List branches of a GitHub repository with protection status. Use to understand the branching strategy.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "count": {"type": "integer", "description": "Number of branches to return (max 100)", "default": 30},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_tags",
            description="List a repository's git tags (most recent first) with the commit SHA each points to.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "count": {"type": "integer", "description": "Number of tags to return (max 100)", "default": 30},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_branch_protection",
            description=(
                "Read a branch's CLASSIC protection rules: required approving reviews, required status checks, "
                "enforce-admins, linear history, force-push and conversation-resolution settings. If the branch "
                "has no classic protection it automatically falls back to reporting any modern Ruleset that "
                "governs it (so a ruleset-protected branch is never wrongly shown as unprotected). For the full "
                "ruleset picture use get_github_branch_rules / get_github_ruleset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "Branch name"},
                },
                "required": ["repo", "branch"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_github_branch",
            description=(
                "Create a Git branch from a base branch or at a commit SHA, before pushing changes; then commit "
                "with create_github_commit or create_or_update_github_file and open a pull request with create_github_pr."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "branch": {"type": "string", "description": "Name for the new branch (e.g. 'fix/typos-in-docs')"},
                    "from_branch": {
                        "type": "string",
                        "description": (
                            "Base branch or commit SHA to create from (default: the repository's default branch, "
                            "read from the repository)"
                        ),
                        "default": "",
                    },
                },
                "required": ["repo", "branch"],
            },
            source=_SOURCE,
            priority=1,
            # A resumed turn's repeat of a branch that landed is answered with it.
            landed_probe=branch_probe(headers),
            landed_key=branch_key,
        ),
        ToolDefinition(
            name="manage_github_ref",
            description=(
                "Manage git refs. action=create_tag creates a lightweight tag at 'sha' (or the head of 'from_ref'); "
                "action=update_branch moves 'branch' to 'sha', a fast-forward only unless force=true, and refuses "
                "(even forced) a move that leaves an open pull request with nothing ahead of its base, which "
                "GitHub answers by closing the pull request; action=delete_branch deletes a branch; "
                "action=delete_tag deletes a tag. delete_branch and delete_tag are permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository in format owner/name"},
                    "action": {"type": "string", "enum": ["create_tag", "update_branch", "delete_branch", "delete_tag"]},
                    "tag": {"type": "string", "description": "Tag name (create_tag/delete_tag)", "default": ""},
                    "branch": {"type": "string", "description": "Branch name (update_branch/delete_branch)", "default": ""},
                    "sha": {
                        "type": "string",
                        "description": "Target commit SHA for create_tag (optional if from_ref given) and update_branch (required)",
                        "default": "",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "update_branch only: allow a move that is not a fast-forward (drops commits)",
                        "default": False,
                    },
                    "from_ref": {
                        "type": "string",
                        "description": "Branch/ref whose head SHA the tag points to (create_tag, default: the repository's default branch)",
                        "default": "",
                    },
                },
                "required": ["repo", "action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
