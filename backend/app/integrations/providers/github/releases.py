"""GitHub releases tools - list/get releases and download their assets, create/update/delete/publish."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.file_delivery import PersistFileFn, deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from . import _common
from ._common import _BASE_URL, _PROVIDER, _coerce_int, _enc, _json_headers, parse_repo

_SOURCE = "integration:github"
_MAX_ASSET_BYTES = 100 * 1024 * 1024
# GitHub's placeholder name for a draft whose tag does not exist yet.
_UNTAGGED_PREFIX = "untagged-"


def _same_ref(asked: str, got: str) -> bool:
    """True when GitHub kept the ref asked for (a short SHA matches its full form)."""
    asked, got = asked.strip(), (got or "").strip()
    if asked == got:
        return True
    return len(asked) >= 7 and len(got) >= 7 and (got.startswith(asked) or asked.startswith(got))


def _unapplied(release: dict[str, Any], *, tag_name: str, target_commitish: str) -> list[str]:
    """The fields a write asked for that the release GitHub answered does not carry."""
    problems: list[str] = []
    got_tag = str(release.get("tag_name") or "")
    if tag_name.strip() and got_tag != tag_name.strip():
        problems.append(f"tag_name {tag_name.strip()!r} asked, GitHub holds {got_tag or 'empty'!r}")
    got_target = str(release.get("target_commitish") or "")
    if target_commitish.strip() and not _same_ref(target_commitish, got_target):
        problems.append(f"target_commitish {target_commitish.strip()!r} asked, GitHub holds {got_target or 'empty'!r}")
    return problems


def _readback(head: str, release: dict[str, Any], latest: bool | None, problems: list[str]) -> str:
    """One answer for create / update / publish: the release as GitHub now holds it."""
    tag = str(release.get("tag_name") or "")
    lines = [
        head,
        f"tag_name: {tag or '(empty)'}" + (" (placeholder: no real tag yet)" if tag.startswith(_UNTAGGED_PREFIX) else ""),
        f"target_commitish: {release.get('target_commitish') or '(empty)'}",
        f"draft: {bool(release.get('draft'))} | prerelease: {bool(release.get('prerelease'))} | latest: "
        + ("unknown" if latest is None else str(latest).lower()),
        f"name: {release.get('name') or '(none)'}",
        f"html_url: {release.get('html_url', '')}",
    ]
    lines.extend(f"NOT APPLIED: {p}" for p in problems)
    return "\n".join(lines)


async def _is_latest(client: Any, base: str, release: dict[str, Any], repo: str, headers: dict[str, str]) -> bool | None:
    """Whether the release is the repository's latest one (None for a draft or when unread)."""
    if release.get("draft") or not release.get("id"):
        return None
    resp, err = await _common.safe_request(
        client, "get", f"{base}/latest", _PROVIDER, f"read latest release in {repo}", headers=headers
    )
    if err or resp is None:
        return None
    return resp.json().get("id") == release.get("id")


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    """Build the GitHub release tools and register their callables."""

    async def list_github_releases(repo: str, count: int = 20) -> str:
        """List a repository's releases (newest first) with tag, status, and date."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/releases",
                _PROVIDER,
                f"list releases in {repo}",
                headers=headers,
                params={"per_page": max(1, min(count, 100))},
            )
            if err:
                return err
            releases = resp.json()
            if not releases:
                return f"No releases found in {repo}."
            lines = []
            for r in releases:
                rel_name = r.get("name") or r.get("tag_name") or "(untitled)"
                tag = r.get("tag_name", "")
                tag_str = f" [{tag}]" if tag and tag != rel_name else ""
                flags = []
                if r.get("draft"):
                    flags.append("DRAFT")
                if r.get("prerelease"):
                    flags.append("PRERELEASE")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                published = (r.get("published_at") or "")[:10]
                date_str = f" published {published}" if published else " (unpublished)"
                url = r.get("html_url", "")
                lines.append(f"{rel_name}{tag_str}{flag_str}{date_str} — {url}")
            return f"Releases in {repo} ({len(releases)}):\n" + "\n".join(lines)

    async def get_github_release(repo: str, tag: str = "", release_id: int = 0) -> str:
        """Get a release by tag or ID, or the latest release when both are omitted. Includes assets."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        if tag.strip():
            url = f"{_BASE_URL}/repos/{owner}/{name}/releases/tags/{_enc(tag)}"
            label = f"release {tag}"
        elif _coerce_int(release_id):
            url = f"{_BASE_URL}/repos/{owner}/{name}/releases/{release_id}"
            label = f"release {release_id}"
        else:
            url = f"{_BASE_URL}/repos/{owner}/{name}/releases/latest"
            label = "latest release"
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"get {label} in {repo}", headers=headers
            )
            if err:
                return err
            r = resp.json()
            flags = []
            if r.get("draft"):
                flags.append("DRAFT")
            if r.get("prerelease"):
                flags.append("PRERELEASE")
            lines = [
                f"Release: {r.get('name') or r.get('tag_name')} (id {r.get('id')}, tag {r.get('tag_name')})"
                + (f" [{', '.join(flags)}]" if flags else ""),
                f"Author: {(r.get('author') or {}).get('login', '?')} | Published: {(r.get('published_at') or 'unpublished')}",
                f"URL: {r.get('html_url', '')}",
            ]
            assets = r.get("assets") or []
            if assets:
                lines.append(f"Assets ({len(assets)}):")
                for a in assets[:30]:
                    size_mb = (a.get("size", 0) or 0) / (1024 * 1024)
                    line = f"  {a.get('name')} ({size_mb:.2f} MB, {a.get('download_count', 0)} downloads)"
                    if a.get("id"):
                        line += f" - asset_id: {a['id']}"
                    if a.get("browser_download_url"):
                        line += f" - download: {a['browser_download_url']}"
                    lines.append(line)
                lines.append("  Tip: download_github_release_asset(repo, asset_id) brings an asset into the conversation.")
            body = (r.get("body") or "").strip()
            if body:
                lines.append("\nNotes:\n" + _common._truncate(body, 4000))
            return "\n".join(lines)

    async def manage_github_release(
        repo: str,
        action: str,
        release_id: int = 0,
        tag_name: str = "",
        name: str = "",
        body: str = "",
        draft: bool | None = None,
        prerelease: bool | None = None,
        target_commitish: str = "",
        generate_release_notes: bool = False,
    ) -> str:
        """Create, update, delete, or publish a release (write / destructive).

        ``action``: ``create`` (needs ``tag_name``), ``update`` (needs
        ``release_id``), ``publish`` (flip a draft live, needs ``release_id``), or
        ``delete`` (needs ``release_id``; permanent). Set ``generate_release_notes``
        to auto-generate notes from merged PRs on create.
        """
        owner, repo_name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        action = (action or "").strip().lower()
        base = f"{_BASE_URL}/repos/{owner}/{repo_name}/releases"
        async with egress_client(timeout=30) as client:
            if action == "create":
                if not tag_name.strip():
                    return "Error: 'tag_name' is required to create a release."
                payload: dict[str, Any] = {"tag_name": tag_name}
                if name:
                    payload["name"] = name
                if body:
                    payload["body"] = body
                if draft is not None:
                    payload["draft"] = bool(draft)
                if prerelease is not None:
                    payload["prerelease"] = bool(prerelease)
                if target_commitish:
                    payload["target_commitish"] = target_commitish
                if generate_release_notes:
                    payload["generate_release_notes"] = True
                resp, err = await _common.safe_request(
                    client, "post", base, _PROVIDER, f"create release in {repo}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                r = resp.json()
                problems = _unapplied(r, tag_name=tag_name, target_commitish=target_commitish)
                latest = await _is_latest(client, base, r, repo, headers)
                verb = "created" if not problems else "created, but GitHub did not apply every field"
                return _readback(f"Release {r.get('id')} {verb} in {owner}/{repo_name}.", r, latest, problems)
            rid = _coerce_int(release_id)
            if not rid:
                return "Error: 'release_id' is required for update/publish/delete (get it from list_github_releases or get_github_release)."
            if action in ("update", "publish"):
                tag = tag_name.strip()
                payload = {}
                if action == "publish":
                    # Read the draft first: GitHub publishes it on whatever tag it holds,
                    # and a draft left on an ``untagged-*`` placeholder ends up released
                    # under that name. Refuse before writing, and send the tag explicitly.
                    cur_resp, err = await _common.safe_request(
                        client, "get", f"{base}/{rid}", _PROVIDER, f"read release {rid} in {repo}", headers=headers
                    )
                    if err:
                        return err
                    current_tag = str(cur_resp.json().get("tag_name") or "")
                    tag = tag or current_tag
                    if not tag or tag.startswith(_UNTAGGED_PREFIX):
                        return (
                            f"Error: release {rid} holds no real tag (tag_name {current_tag or 'empty'!r}); "
                            "publishing it would release it under that name. Pass tag_name to publish it on a tag."
                        )
                    payload["draft"] = False
                if tag:
                    payload["tag_name"] = tag
                if name:
                    payload["name"] = name
                if body:
                    payload["body"] = body
                if target_commitish.strip():
                    payload["target_commitish"] = target_commitish.strip()
                if draft is not None and action == "update":
                    payload["draft"] = bool(draft)
                if prerelease is not None:
                    payload["prerelease"] = bool(prerelease)
                if not payload:
                    return "No fields to update. Provide name, body, tag_name, target_commitish, draft, or prerelease."
                resp, err = await _common.safe_request(
                    client, "patch", f"{base}/{rid}", _PROVIDER, f"{action} release {rid} in {repo}", headers=_json_headers(headers), json=payload
                )
                if err:
                    return err
                r = resp.json()
                problems = _unapplied(r, tag_name=tag, target_commitish=target_commitish)
                if action == "publish" and r.get("draft"):
                    problems.append("the release is still a draft")
                latest = await _is_latest(client, base, r, repo, headers)
                done = "published" if action == "publish" else "updated"
                verb = done if not problems else f"{done}, but GitHub did not apply every field"
                return _readback(f"Release {rid} {verb} in {owner}/{repo_name}.", r, latest, problems)
            if action == "delete":
                resp, err = await _common.safe_request(
                    client, "delete", f"{base}/{rid}", _PROVIDER, f"delete release {rid} in {repo}", headers=headers
                )
                if err:
                    return err
                return f"Release {rid} permanently deleted from {owner}/{repo_name}."
            return "Error: invalid action. Use create, update, publish, or delete."

    async def download_github_release_asset(repo: str, asset_id: int) -> str | ToolResult:
        """Download a release asset into the conversation (stored file + links + file_id)."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        aid = _coerce_int(asset_id)
        if not aid:
            return "Error: 'asset_id' is required (listed under the assets of get_github_release)."
        url = f"{_BASE_URL}/repos/{owner}/{name}/releases/assets/{aid}"
        # The asset is served by a redirect to a signed download host;
        # httpx drops the Authorization header on that cross-origin hop.
        async with egress_client(timeout=120, follow_redirects=True) as client:
            resp, err = await _common.safe_request(
                client, "get", url, _PROVIDER, f"get release asset {aid} in {repo}", headers=headers
            )
            if err:
                return err
            meta = resp.json()
            size = int(meta.get("size") or 0)
            if size > _MAX_ASSET_BYTES:
                return (
                    f"Asset {meta.get('name')} is {round(size / 1024 / 1024, 1)} MB, over the "
                    f"{_MAX_ASSET_BYTES // 1024 // 1024} MB download limit. Download URL: "
                    f"{meta.get('browser_download_url', '')}"
                )
            raw, err = await _common.safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                f"download release asset {aid} in {repo}",
                headers={**headers, "Accept": "application/octet-stream"},
            )
            if err:
                return err
        return await deliver_file(
            data=raw.content,
            filename=meta.get("name") or f"github-asset-{aid}",
            content_type=meta.get("content_type") or "application/octet-stream",
            source="GitHub",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", meta.get("browser_download_url"))],
        )

    executor.register("list_github_releases", list_github_releases)
    executor.register("get_github_release", get_github_release)
    executor.register("download_github_release_asset", download_github_release_asset)
    executor.register("manage_github_release", manage_github_release)

    _repo = {"type": "string", "description": "Repository in format owner/name"}

    return [
        ToolDefinition(
            name="list_github_releases",
            description=(
                "List a GitHub repository's releases (newest first). Shows release name/tag, draft/prerelease status, "
                "publish date, and URL. Use for release notes or syncing release info."
            ),
            input_schema={
                "type": "object",
                "properties": {"repo": _repo, "count": {"type": "integer", "description": "Number of releases to return (max 100)", "default": 20}},
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_release",
            description="Get a release by tag or numeric ID, or the latest published release when both are omitted. Includes release notes and download assets.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "tag": {"type": "string", "description": "Release tag (e.g. 'v1.2.3'); omit with release_id for the latest release", "default": ""},
                    "release_id": {"type": "integer", "description": "Numeric release ID (alternative to tag)", "default": 0},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_github_release_asset",
            workspace_ingest=True,
            description=(
                "Download a release asset (asset_id from get_github_release) into the "
                "conversation: a download card, its download URL and a file_id to forward "
                "to any tool that takes a file. Its readable content comes with the result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "asset_id": {"type": "integer", "description": "Asset ID listed by get_github_release"},
                },
                "required": ["repo", "asset_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_github_release",
            description=(
                "Create, update, publish, or delete a release. action=create (needs tag_name; set "
                "generate_release_notes to auto-build notes from merged PRs), update (needs release_id), publish "
                "(flip a draft live, needs release_id), or delete (needs release_id; permanent). "
                "Use draft=true to stage a release and prerelease=true for a pre-release."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "action": {"type": "string", "enum": ["create", "update", "publish", "delete"]},
                    "release_id": {"type": "integer", "description": "Release ID (update/publish/delete)", "default": 0},
                    "tag_name": {"type": "string", "description": "Tag to release (create; or rename on update)", "default": ""},
                    "name": {"type": "string", "description": "Release title", "default": ""},
                    "body": {"type": "string", "description": "Release notes (Markdown)", "default": ""},
                    "draft": {"type": "boolean", "description": "Draft state"},
                    "prerelease": {"type": "boolean", "description": "Pre-release flag"},
                    "target_commitish": {"type": "string", "description": "Branch/commit the tag is created from (create, or update of a draft)", "default": ""},
                    "generate_release_notes": {"type": "boolean", "description": "Auto-generate notes from merged PRs (create)", "default": False},
                },
                "required": ["repo", "action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
