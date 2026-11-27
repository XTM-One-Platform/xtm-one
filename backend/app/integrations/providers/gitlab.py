# SPDX-License-Identifier: Apache-2.0

"""GitLab integration - remediation issues and merge requests for DevSecOps teams.

GitLab.com or a self-managed instance, through the REST API v4:

  - Projects: list (search, membership, a user's), read one.
  - Issues: list (state, labels, search, assignee), read, create, edit (close,
    reopen, labels, assignees, due date, lock), notes (list, read one, add).
  - Merge requests: list, read, add a note.
  - Repository files: list a tree, read a file, create / update / delete one.
  - Releases: list, read, create, update, delete.

Authentication is a personal, project or group access token sent in the
``PRIVATE-TOKEN`` header. The instance is ``https://gitlab.com`` unless the
integration sets a ``base_url`` (an http(s) URL with no credentials, query or
fragment, see :func:`~app.integrations.http.operator_base_url`); the API lives
under ``/api/v4``.
A single-item read (an issue, a merge request, a release, a comment) shows its
text whole; the notes listing cuts a long comment through ``clip_text`` with the
``get_gitlab_issue_note`` call that reads it.
A project is named by its numeric id or its ``group/project`` path, which is
URL-encoded whole into a single path segment, like every file path and tag.

Token format: ``base_url||token`` (``base_url`` optional, from extra_config).
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    clip_text,
    operator_base_url,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "GitLab"
_SOURCE = "integration:gitlab"

GITLAB_DEFAULT_URL = "https://gitlab.com"

_TIMEOUT = 30
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
#: Largest repository file read into the conversation as text.
_MAX_FILE_CHARS = 60_000
#: Head of one comment a notes listing shows; get_gitlab_issue_note reads it whole.
_LISTING_NOTE_CHARS = 2000

_PROJECT_PATH_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,254}")
_ISSUE_STATES = ("opened", "closed", "all")
_MR_STATES = ("opened", "closed", "merged", "locked", "all")


def gitlab_api_base(base_url: str) -> str | None:
    """``<instance>/api/v4`` for *base_url* (GitLab.com when blank), ``None`` when invalid."""
    instance = operator_base_url((base_url or "").strip() or GITLAB_DEFAULT_URL, strip_suffix="/api/v4")
    return f"{instance}/api/v4" if instance else None


def build_gitlab_headers(token: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    return {"PRIVATE-TOKEN": (token or "").strip(), "Accept": "application/json"}


def project_ref(project: Any) -> str | None:
    """*project* (numeric id or ``group/project`` path) as one encoded path segment."""
    text = str(project or "").strip().strip("/")
    if text.isdigit():
        return text
    if not _PROJECT_PATH_RE.fullmatch(text) or any(seg in ("", ".", "..") for seg in text.split("/")):
        return None
    return quote(text, safe="")


def file_ref(path: Any) -> str | None:
    """A repository file path as one encoded path segment, ``None`` when unsafe."""
    text = str(path or "").strip().lstrip("/")
    if not text or len(text) > 1024 or any(seg in ("", ".", "..") for seg in text.split("/")):
        return None
    if any(ord(c) < 32 for c in text):
        return None
    return quote(text, safe="")


def _clean(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _bounded(value: Any, default: int, ceiling: int, floor: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(floor, min(n, ceiling))


def _iid(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _note_id(note: dict[str, Any]) -> str:
    nid = _iid(note.get("id"))
    return str(nid) if nid is not None else "?"


def _labels(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _people(items: Any) -> str:
    names = [_clean(p.get("username"), 60) for p in items or [] if isinstance(p, dict)]
    return ", ".join(names) or "-"


def _format_issue(issue: dict[str, Any], *, detail: bool = False) -> str:
    lines = [
        f"#{issue.get('iid', '?')} {_clean(issue.get('title'))} [{_clean(issue.get('state'), 20)}]",
        f"  Labels: {', '.join(_clean(x, 60) for x in issue.get('labels') or []) or '-'}"
        f" | Assignees: {_people(issue.get('assignees'))}"
        + (f" | Due: {_clean(issue.get('due_date'), 20)}" if issue.get("due_date") else ""),
    ]
    if detail:
        lines.append(
            f"  Author: {_people([issue.get('author')])} | Created: {_clean(issue.get('created_at'), 40)}"
            f" | Updated: {_clean(issue.get('updated_at'), 40)}"
            + (" | Confidential" if issue.get("confidential") else "")
        )
        if issue.get("description"):
            lines.append("  Description:\n" + str(issue["description"]))
    if issue.get("web_url"):
        lines.append(f"  Link: {_clean(issue.get('web_url'), 300)}")
    return "\n".join(lines)


def _format_mr(mr: dict[str, Any], *, detail: bool = False) -> str:
    lines = [
        f"!{mr.get('iid', '?')} {_clean(mr.get('title'))} [{_clean(mr.get('state'), 20)}]",
        f"  {_clean(mr.get('source_branch'), 100)} -> {_clean(mr.get('target_branch'), 100)}"
        f" | Author: {_people([mr.get('author')])} | Reviewers: {_people(mr.get('reviewers'))}",
    ]
    if detail:
        lines.append(
            f"  Merge status: {_clean(mr.get('detailed_merge_status') or mr.get('merge_status'), 40)}"
            f" | Draft: {'yes' if mr.get('draft') else 'no'} | Updated: {_clean(mr.get('updated_at'), 40)}"
        )
        if mr.get("description"):
            lines.append("  Description:\n" + str(mr["description"]))
    if mr.get("web_url"):
        lines.append(f"  Link: {_clean(mr.get('web_url'), 300)}")
    return "\n".join(lines)


def _format_release(rel: dict[str, Any], *, detail: bool = False) -> str:
    line = (
        f"{_clean(rel.get('tag_name'), 100)}: {_clean(rel.get('name'))}"
        f" (released {_clean(rel.get('released_at'), 40)})"
    )
    if detail and rel.get("description"):
        line += "\n  Notes:\n" + str(rel["description"])
    return line


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    base_url, access_token = parse_compound_token(token, 2, greedy_last=True)
    if "||" not in (token or ""):
        base_url, access_token = "", token or ""
    access_token = access_token.strip()
    api = gitlab_api_base(base_url)
    if not access_token or api is None:
        return []
    headers = build_gitlab_headers(access_token)

    async def _get(path: str, action: str, params: dict | None = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client,
                "get",
                f"{api}{path}",
                _PROVIDER,
                action,
                headers=headers,
                params=params,
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )

    async def _write(method: str, path: str, action: str, body: dict | None = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client, method, f"{api}{path}", _PROVIDER, action, headers=headers, json=body
            )

    def _bad_project() -> str:
        return "Error: project must be a numeric project id or its 'group/project' path."

    def _page(limit: Any, page: Any) -> dict[str, int]:
        return {
            "per_page": _bounded(limit, _DEFAULT_LIMIT, _MAX_LIMIT),
            "page": _bounded(page, 1, 10_000),
        }

    def _next(resp: httpx.Response) -> str:
        nxt = resp.headers.get("x-next-page", "").strip()
        return f"\n[More results: pass page={nxt}]" if nxt.isdigit() else ""

    def _list(data: Any) -> list[dict[str, Any]]:
        return [x for x in data or [] if isinstance(x, dict)] if isinstance(data, list) else []

    # -- Projects ---------------------------------------------------------

    async def list_gitlab_projects(
        search: str = "", membership: bool = True, username: str = "", limit: int = _DEFAULT_LIMIT, page: int = 1
    ) -> str:
        params: dict[str, Any] = {**_page(limit, page), "order_by": "last_activity_at", "simple": "true"}
        if search:
            params["search"] = str(search)[:100]
        path = "/projects"
        if username:
            user = str(username).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", user):
                return "Error: username must be a GitLab username."
            path = f"/users/{quote(user, safe='')}/projects"
        elif membership:
            params["membership"] = "true"
        resp, err = await _get(path, "list projects", params)
        if err:
            return err
        projects = _list(_json(resp))
        if not projects:
            return "No projects found."
        lines = [
            f"- {_clean(p.get('path_with_namespace'), 200)} (id: {p.get('id', '?')}): "
            f"default branch {_clean(p.get('default_branch'), 100)}, "
            f"last activity {_clean(p.get('last_activity_at'), 40)}"
            for p in projects
        ]
        return f"Projects ({len(projects)}):\n" + "\n".join(lines) + _next(resp)

    async def get_gitlab_project(project: str) -> str:
        ref = project_ref(project)
        if not ref:
            return _bad_project()
        resp, err = await _get(f"/projects/{ref}", f"get project {project}")
        if err:
            return err
        p = _json(resp) or {}
        return (
            f"{_clean(p.get('path_with_namespace'), 200)} (id: {p.get('id', '?')})\n"
            f"  Visibility: {_clean(p.get('visibility'), 20)} | Default branch: {_clean(p.get('default_branch'), 100)}"
            f" | Open issues: {p.get('open_issues_count', '?')} | Archived: {'yes' if p.get('archived') else 'no'}\n"
            f"  Description: {_clean(p.get('description'), 500)}\n"
            f"  Link: {_clean(p.get('web_url'), 300)}"
        )

    # -- Issues -----------------------------------------------------------

    async def list_gitlab_issues(
        project: str,
        state: str = "opened",
        labels: list[str] | str | None = None,
        search: str = "",
        assignee_username: str = "",
        limit: int = _DEFAULT_LIMIT,
        page: int = 1,
    ) -> str:
        ref = project_ref(project)
        if not ref:
            return _bad_project()
        if state not in _ISSUE_STATES:
            return f"Error: state must be one of {', '.join(_ISSUE_STATES)}."
        params: dict[str, Any] = {**_page(limit, page), "state": state}
        if labels:
            params["labels"] = _labels(labels)
        if search:
            params["search"] = str(search)[:200]
        if assignee_username:
            params["assignee_username"] = str(assignee_username)[:255]
        resp, err = await _get(f"/projects/{ref}/issues", f"list issues of {project}", params)
        if err:
            return err
        issues = _list(_json(resp))
        if not issues:
            return "No issues found."
        return f"Issues ({len(issues)}):\n" + "\n".join(_format_issue(i) for i in issues) + _next(resp)

    async def get_gitlab_issue(project: str, issue_iid: int) -> str:
        ref, iid = project_ref(project), _iid(issue_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: issue_iid must be the issue's number in the project (#12 -> 12)."
        resp, err = await _get(f"/projects/{ref}/issues/{iid}", f"get issue #{iid}")
        if err:
            return err
        return _format_issue(_json(resp) or {}, detail=True)

    async def create_gitlab_issue(
        project: str,
        title: str,
        description: str = "",
        labels: list[str] | str | None = None,
        assignee_ids: list[int] | None = None,
        due_date: str = "",
        confidential: bool = False,
    ) -> str:
        ref = project_ref(project)
        if not ref:
            return _bad_project()
        head = " ".join(str(title or "").split())
        if not head:
            return "Error: title is required."
        body: dict[str, Any] = {"title": head[:255]}
        if description:
            body["description"] = str(description)[:1_000_000]
        if labels:
            body["labels"] = _labels(labels)
        if assignee_ids:
            ids = [_iid(a) for a in assignee_ids]
            if None in ids:
                return "Error: assignee_ids must be numeric GitLab user ids."
            body["assignee_ids"] = ids
        if due_date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due_date)):
                return "Error: due_date must be YYYY-MM-DD."
            body["due_date"] = due_date
        if confidential:
            body["confidential"] = True
        resp, err = await _write("post", f"/projects/{ref}/issues", f"create issue in {project}", body)
        if err:
            return err
        return "Created issue:\n" + _format_issue(_json(resp) or {})

    async def update_gitlab_issue(
        project: str,
        issue_iid: int,
        title: str = "",
        description: str | None = None,
        state_event: str = "",
        labels: list[str] | str | None = None,
        add_labels: list[str] | str | None = None,
        remove_labels: list[str] | str | None = None,
        assignee_ids: list[int] | None = None,
        due_date: str = "",
        discussion_locked: bool | None = None,
    ) -> str:
        ref, iid = project_ref(project), _iid(issue_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: issue_iid must be the issue's number in the project (#12 -> 12)."
        body: dict[str, Any] = {}
        if title:
            body["title"] = " ".join(str(title).split())[:255]
        if description is not None:
            body["description"] = str(description)[:1_000_000]
        if state_event:
            if state_event not in ("close", "reopen"):
                return "Error: state_event must be close or reopen."
            body["state_event"] = state_event
        for key, value in (("labels", labels), ("add_labels", add_labels), ("remove_labels", remove_labels)):
            if value is not None:
                body[key] = _labels(value)
        if assignee_ids is not None:
            ids = [_iid(a) for a in assignee_ids]
            if None in ids:
                return "Error: assignee_ids must be numeric GitLab user ids."
            body["assignee_ids"] = ids
        if due_date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due_date)):
                return "Error: due_date must be YYYY-MM-DD."
            body["due_date"] = due_date
        if discussion_locked is not None:
            body["discussion_locked"] = bool(discussion_locked)
        if not body:
            return "Error: nothing to change."
        resp, err = await _write("put", f"/projects/{ref}/issues/{iid}", f"update issue #{iid}", body)
        if err:
            return err
        return "Updated issue:\n" + _format_issue(_json(resp) or {})

    async def list_gitlab_issue_notes(project: str, issue_iid: int, limit: int = _DEFAULT_LIMIT, page: int = 1) -> str:
        ref, iid = project_ref(project), _iid(issue_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: issue_iid must be the issue's number in the project (#12 -> 12)."
        params = {**_page(limit, page), "sort": "asc"}
        resp, err = await _get(f"/projects/{ref}/issues/{iid}/notes", f"list notes of issue #{iid}", params)
        if err:
            return err
        notes = _list(_json(resp))
        if not notes:
            return f"Issue #{iid} has no comments."
        lines = [
            f"- {_clean(n.get('created_at'), 40)} {_people([n.get('author')])}"
            + (" (system)" if n.get("system") else "")
            + f" [note {_note_id(n)}]: "
            + clip_text(
                " ".join(str(n.get("body") or "").split()) or "-",
                _LISTING_NOTE_CHARS,
                f"get_gitlab_issue_note(project={project!r}, issue_iid={iid}, note_id={_note_id(n)})",
            )
            for n in notes
        ]
        return f"Comments on #{iid} ({len(notes)}):\n" + "\n".join(lines) + _next(resp)

    async def get_gitlab_issue_note(project: str, issue_iid: int, note_id: int) -> str:
        ref, iid, nid = project_ref(project), _iid(issue_iid), _iid(note_id)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: issue_iid must be the issue's number in the project (#12 -> 12)."
        if nid is None:
            return "Error: note_id must be the comment's id, as list_gitlab_issue_notes shows it."
        resp, err = await _get(f"/projects/{ref}/issues/{iid}/notes/{nid}", f"get note {nid} of issue #{iid}")
        if err:
            return err
        note = _json(resp) or {}
        return (
            f"Comment {_note_id(note)} on #{iid}, {_clean(note.get('created_at'), 40)} {_people([note.get('author')])}"
            + (" (system)" if note.get("system") else "")
            + ":\n"
            + (str(note.get("body") or "") or "-")
        )

    async def add_gitlab_issue_note(project: str, issue_iid: int, body: str) -> str:
        ref, iid = project_ref(project), _iid(issue_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: issue_iid must be the issue's number in the project (#12 -> 12)."
        text = str(body or "").strip()
        if not text:
            return "Error: body is required."
        resp, err = await _write(
            "post", f"/projects/{ref}/issues/{iid}/notes", f"comment on issue #{iid}", {"body": text[:1_000_000]}
        )
        if err:
            return err
        return f"Commented on issue #{iid} (note id: {(_json(resp) or {}).get('id', '?')})."

    # -- Merge requests -----------------------------------------------------

    async def list_gitlab_merge_requests(
        project: str, state: str = "opened", labels: list[str] | str | None = None,
        search: str = "", limit: int = _DEFAULT_LIMIT, page: int = 1,
    ) -> str:
        ref = project_ref(project)
        if not ref:
            return _bad_project()
        if state not in _MR_STATES:
            return f"Error: state must be one of {', '.join(_MR_STATES)}."
        params: dict[str, Any] = {**_page(limit, page), "state": state}
        if labels:
            params["labels"] = _labels(labels)
        if search:
            params["search"] = str(search)[:200]
        resp, err = await _get(f"/projects/{ref}/merge_requests", f"list merge requests of {project}", params)
        if err:
            return err
        mrs = _list(_json(resp))
        if not mrs:
            return "No merge requests found."
        return f"Merge requests ({len(mrs)}):\n" + "\n".join(_format_mr(m) for m in mrs) + _next(resp)

    async def get_gitlab_merge_request(project: str, merge_request_iid: int) -> str:
        ref, iid = project_ref(project), _iid(merge_request_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: merge_request_iid must be the merge request's number (!7 -> 7)."
        resp, err = await _get(f"/projects/{ref}/merge_requests/{iid}", f"get merge request !{iid}")
        if err:
            return err
        return _format_mr(_json(resp) or {}, detail=True)

    async def add_gitlab_merge_request_note(project: str, merge_request_iid: int, body: str) -> str:
        ref, iid = project_ref(project), _iid(merge_request_iid)
        if not ref:
            return _bad_project()
        if iid is None:
            return "Error: merge_request_iid must be the merge request's number (!7 -> 7)."
        text = str(body or "").strip()
        if not text:
            return "Error: body is required."
        _resp, err = await _write(
            "post", f"/projects/{ref}/merge_requests/{iid}/notes", f"comment on merge request !{iid}",
            {"body": text[:1_000_000]},
        )
        return err or f"Commented on merge request !{iid}."

    # -- Repository files ---------------------------------------------------

    async def list_gitlab_repository_tree(
        project: str, path: str = "", ref: str = "", recursive: bool = False, limit: int = 100, page: int = 1
    ) -> str:
        pref = project_ref(project)
        if not pref:
            return _bad_project()
        params: dict[str, Any] = {**_page(limit, page)}
        if path:
            if file_ref(path) is None:
                return "Error: path must be a repository path without '.' or '..' segments."
            params["path"] = str(path).strip().strip("/")
        if ref:
            params["ref"] = str(ref)[:255]
        if recursive:
            params["recursive"] = "true"
        resp, err = await _get(f"/projects/{pref}/repository/tree", f"list the tree of {project}", params)
        if err:
            return err
        items = _list(_json(resp))
        if not items:
            return "No files found."
        lines = [f"- {_clean(i.get('path'), 500)}{'/' if i.get('type') == 'tree' else ''}" for i in items]
        return f"Entries ({len(items)}):\n" + "\n".join(lines) + _next(resp)

    async def get_gitlab_file(project: str, file_path: str, ref: str = "") -> str:
        pref, fref = project_ref(project), file_ref(file_path)
        if not pref:
            return _bad_project()
        if not fref:
            return "Error: file_path must be a repository path without '.' or '..' segments."
        resp, err = await _get(
            f"/projects/{pref}/repository/files/{fref}", f"read {file_path}", {"ref": str(ref or "HEAD")[:255]}
        )
        if err:
            return err
        data = _json(resp) or {}
        try:
            text = base64.b64decode(data.get("content") or "").decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return f"{_clean(data.get('file_path'), 500)} is a binary file ({data.get('size', '?')} bytes): not shown."
        head = (
            f"{_clean(data.get('file_path'), 500)} at {_clean(data.get('ref'), 255)} "
            f"(blob {_clean(data.get('blob_id'), 64)}, last commit {_clean(data.get('last_commit_id'), 64)}):\n"
        )
        return head + clip_text(text, _MAX_FILE_CHARS)

    async def _file_write(method: str, project: str, file_path: str, branch: str, commit_message: str,
                          content: str | None, verb: str) -> str:
        pref, fref = project_ref(project), file_ref(file_path)
        if not pref:
            return _bad_project()
        if not fref:
            return "Error: file_path must be a repository path without '.' or '..' segments."
        if not str(branch or "").strip():
            return "Error: branch is required."
        message = str(commit_message or "").strip()
        if not message:
            return "Error: commit_message is required."
        body: dict[str, Any] = {"branch": str(branch).strip()[:255], "commit_message": message[:10_000]}
        if content is not None:
            body["content"] = str(content)
        _resp, err = await _write(method, f"/projects/{pref}/repository/files/{fref}", f"{verb} {file_path}", body)
        if err:
            return err
        return f"{verb.capitalize()}d {file_path} on {body['branch']}."

    async def create_gitlab_file(project: str, file_path: str, branch: str, content: str, commit_message: str) -> str:
        return await _file_write("post", project, file_path, branch, commit_message, content, "create")

    async def update_gitlab_file(project: str, file_path: str, branch: str, content: str, commit_message: str) -> str:
        return await _file_write("put", project, file_path, branch, commit_message, content, "update")

    async def delete_gitlab_file(project: str, file_path: str, branch: str, commit_message: str) -> str:
        return await _file_write("delete", project, file_path, branch, commit_message, None, "delete")

    # -- Releases -----------------------------------------------------------

    def _tag(tag_name: Any) -> str | None:
        text = str(tag_name or "").strip()
        if not text or len(text) > 255 or ".." in text or any(ord(c) < 33 for c in text):
            return None
        return quote(text, safe="")

    async def list_gitlab_releases(project: str, limit: int = _DEFAULT_LIMIT, page: int = 1) -> str:
        ref = project_ref(project)
        if not ref:
            return _bad_project()
        resp, err = await _get(f"/projects/{ref}/releases", f"list releases of {project}", _page(limit, page))
        if err:
            return err
        releases = _list(_json(resp))
        if not releases:
            return "No releases found."
        return f"Releases ({len(releases)}):\n" + "\n".join(f"- {_format_release(r)}" for r in releases) + _next(resp)

    async def get_gitlab_release(project: str, tag_name: str) -> str:
        ref, tag = project_ref(project), _tag(tag_name)
        if not ref:
            return _bad_project()
        if not tag:
            return "Error: tag_name must be a tag name."
        resp, err = await _get(f"/projects/{ref}/releases/{tag}", f"get release {tag_name}")
        if err:
            return err
        return _format_release(_json(resp) or {}, detail=True)

    async def create_gitlab_release(
        project: str, tag_name: str, name: str = "", description: str = "", ref: str = ""
    ) -> str:
        pref = project_ref(project)
        if not pref:
            return _bad_project()
        if not _tag(tag_name):
            return "Error: tag_name must be a tag name."
        body: dict[str, Any] = {"tag_name": str(tag_name).strip()}
        if name:
            body["name"] = str(name)[:255]
        if description:
            body["description"] = str(description)[:1_000_000]
        if ref:
            body["ref"] = str(ref)[:255]
        resp, err = await _write("post", f"/projects/{pref}/releases", f"create release {tag_name}", body)
        if err:
            return err
        return "Created release " + _format_release(_json(resp) or {})

    async def update_gitlab_release(project: str, tag_name: str, name: str = "", description: str | None = None) -> str:
        pref, tag = project_ref(project), _tag(tag_name)
        if not pref:
            return _bad_project()
        if not tag:
            return "Error: tag_name must be a tag name."
        body: dict[str, Any] = {}
        if name:
            body["name"] = str(name)[:255]
        if description is not None:
            body["description"] = str(description)[:1_000_000]
        if not body:
            return "Error: nothing to change."
        resp, err = await _write("put", f"/projects/{pref}/releases/{tag}", f"update release {tag_name}", body)
        if err:
            return err
        return "Updated release " + _format_release(_json(resp) or {})

    async def delete_gitlab_release(project: str, tag_name: str) -> str:
        pref, tag = project_ref(project), _tag(tag_name)
        if not pref:
            return _bad_project()
        if not tag:
            return "Error: tag_name must be a tag name."
        _resp, err = await _write("delete", f"/projects/{pref}/releases/{tag}", f"delete release {tag_name}")
        return err or f"Deleted release {str(tag_name).strip()} (the tag stays)."

    handlers = {
        "list_gitlab_projects": list_gitlab_projects,
        "get_gitlab_project": get_gitlab_project,
        "list_gitlab_issues": list_gitlab_issues,
        "get_gitlab_issue": get_gitlab_issue,
        "create_gitlab_issue": create_gitlab_issue,
        "update_gitlab_issue": update_gitlab_issue,
        "list_gitlab_issue_notes": list_gitlab_issue_notes,
        "get_gitlab_issue_note": get_gitlab_issue_note,
        "add_gitlab_issue_note": add_gitlab_issue_note,
        "list_gitlab_merge_requests": list_gitlab_merge_requests,
        "get_gitlab_merge_request": get_gitlab_merge_request,
        "add_gitlab_merge_request_note": add_gitlab_merge_request_note,
        "list_gitlab_repository_tree": list_gitlab_repository_tree,
        "get_gitlab_file": get_gitlab_file,
        "create_gitlab_file": create_gitlab_file,
        "update_gitlab_file": update_gitlab_file,
        "delete_gitlab_file": delete_gitlab_file,
        "list_gitlab_releases": list_gitlab_releases,
        "get_gitlab_release": get_gitlab_release,
        "create_gitlab_release": create_gitlab_release,
        "update_gitlab_release": update_gitlab_release,
        "delete_gitlab_release": delete_gitlab_release,
    }
    for tool_name, fn in handlers.items():
        executor.register(tool_name, fn)

    def _s(description: str) -> dict:
        return {"type": "string", "description": description}

    def _i(description: str) -> dict:
        return {"type": "integer", "description": description}

    project_p = _s("Project: numeric id or 'group/project' path")
    issue_p = _i("Issue number in the project (#12 -> 12)")
    mr_p = _i("Merge request number in the project (!7 -> 7)")
    labels_p = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Label names",
    }
    limit_p = _i(f"Items per page (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})")
    page_p = _i("Page number, from the previous answer's hint")
    file_p = _s("File path in the repository (e.g. config/app.yml)")
    tag_p = _s("Tag name of the release (e.g. v1.4.2)")

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "list_gitlab_projects",
            "List GitLab projects the token is a member of (or a user's projects), most recently active first.",
            {
                "search": _s("Part of the project name"),
                "membership": {"type": "boolean", "description": "Only projects the token's user belongs to (default true)"},
                "username": _s("List this user's projects instead"),
                "limit": limit_p,
                "page": page_p,
            },
            [],
        ),
        _tool("get_gitlab_project", "Read a GitLab project: visibility, default branch, open issues, link.",
              {"project": project_p}, ["project"]),
        _tool(
            "list_gitlab_issues",
            "List a GitLab project's issues by state, labels, text and assignee: the remediation backlog.",
            {
                "project": project_p,
                "state": {"type": "string", "enum": list(_ISSUE_STATES), "description": "Default opened"},
                "labels": labels_p,
                "search": _s("Text in the title or description"),
                "assignee_username": _s("Assignee username"),
                "limit": limit_p,
                "page": page_p,
            },
            ["project"],
        ),
        _tool("get_gitlab_issue", "Read a GitLab issue with its whole description, labels, assignees and dates.",
              {"project": project_p, "issue_iid": issue_p}, ["project", "issue_iid"]),
        _tool(
            "create_gitlab_issue",
            "Open a GitLab issue, e.g. a remediation ticket for a vulnerable component or exposed service.",
            {
                "project": project_p,
                "title": _s("Issue title"),
                "description": _s("Markdown description"),
                "labels": labels_p,
                "assignee_ids": {"type": "array", "items": {"type": "integer"}, "description": "GitLab user ids"},
                "due_date": _s("YYYY-MM-DD"),
                "confidential": {"type": "boolean", "description": "Visible to project members only"},
            },
            ["project", "title"],
        ),
        _tool(
            "update_gitlab_issue",
            "Edit a GitLab issue: title, description, close or reopen, labels (replace, add, remove), "
            "assignees, due date, lock the discussion.",
            {
                "project": project_p,
                "issue_iid": issue_p,
                "title": _s("New title"),
                "description": _s("New description"),
                "state_event": {"type": "string", "enum": ["close", "reopen"]},
                "labels": {**labels_p, "description": "Replace every label"},
                "add_labels": labels_p,
                "remove_labels": labels_p,
                "assignee_ids": {"type": "array", "items": {"type": "integer"}, "description": "Replace the assignees ([] unassigns)"},
                "due_date": _s("YYYY-MM-DD"),
                "discussion_locked": {"type": "boolean", "description": "Lock or unlock the discussion"},
            },
            ["project", "issue_iid"],
        ),
        _tool("list_gitlab_issue_notes",
              "List the comments of a GitLab issue, oldest first, each on one line with its note id "
              f"(a comment longer than {_LISTING_NOTE_CHARS} characters is cut: get_gitlab_issue_note reads it whole).",
              {"project": project_p, "issue_iid": issue_p, "limit": limit_p, "page": page_p}, ["project", "issue_iid"]),
        _tool("get_gitlab_issue_note", "Read one comment of a GitLab issue whole, by the note id the listing shows.",
              {"project": project_p, "issue_iid": issue_p, "note_id": _i("Note id from list_gitlab_issue_notes")},
              ["project", "issue_iid", "note_id"]),
        _tool("add_gitlab_issue_note", "Comment on a GitLab issue.",
              {"project": project_p, "issue_iid": issue_p, "body": _s("Markdown comment")},
              ["project", "issue_iid", "body"]),
        _tool(
            "list_gitlab_merge_requests",
            "List a GitLab project's merge requests by state, labels and text.",
            {
                "project": project_p,
                "state": {"type": "string", "enum": list(_MR_STATES), "description": "Default opened"},
                "labels": labels_p,
                "search": _s("Text in the title or description"),
                "limit": limit_p,
                "page": page_p,
            },
            ["project"],
        ),
        _tool("get_gitlab_merge_request", "Read a GitLab merge request: branches, merge status, reviewers, whole description.",
              {"project": project_p, "merge_request_iid": mr_p}, ["project", "merge_request_iid"]),
        _tool("add_gitlab_merge_request_note", "Comment on a GitLab merge request.",
              {"project": project_p, "merge_request_iid": mr_p, "body": _s("Markdown comment")},
              ["project", "merge_request_iid", "body"]),
        _tool(
            "list_gitlab_repository_tree",
            "List the files and folders of a GitLab repository path.",
            {
                "project": project_p,
                "path": _s("Folder (default: the root)"),
                "ref": _s("Branch, tag or commit (default: the default branch)"),
                "recursive": {"type": "boolean"},
                "limit": limit_p,
                "page": page_p,
            },
            ["project"],
        ),
        _tool("get_gitlab_file", "Read a text file of a GitLab repository (e.g. a dependency manifest or a config).",
              {"project": project_p, "file_path": file_p, "ref": _s("Branch, tag or commit (default HEAD)")},
              ["project", "file_path"]),
        _tool(
            "create_gitlab_file",
            "Commit a new file to a branch of a GitLab repository.",
            {"project": project_p, "file_path": file_p, "branch": _s("Branch"), "content": _s("File content"),
             "commit_message": _s("Commit message")},
            ["project", "file_path", "branch", "content", "commit_message"],
        ),
        _tool(
            "update_gitlab_file",
            "Commit new content for an existing file on a branch of a GitLab repository (e.g. bump a vulnerable dependency).",
            {"project": project_p, "file_path": file_p, "branch": _s("Branch"), "content": _s("Whole new file content"),
             "commit_message": _s("Commit message")},
            ["project", "file_path", "branch", "content", "commit_message"],
        ),
        _tool(
            "delete_gitlab_file",
            "Commit the deletion of a file on a branch of a GitLab repository.",
            {"project": project_p, "file_path": file_p, "branch": _s("Branch"), "commit_message": _s("Commit message")},
            ["project", "file_path", "branch", "commit_message"],
        ),
        _tool("list_gitlab_releases", "List a GitLab project's releases, newest first.",
              {"project": project_p, "limit": limit_p, "page": page_p}, ["project"]),
        _tool("get_gitlab_release", "Read a GitLab release and its notes.",
              {"project": project_p, "tag_name": tag_p}, ["project", "tag_name"]),
        _tool(
            "create_gitlab_release",
            "Create a GitLab release on a tag (created from ref when the tag does not exist).",
            {"project": project_p, "tag_name": tag_p, "name": _s("Release title"),
             "description": _s("Release notes (markdown)"), "ref": _s("Branch or commit for a new tag")},
            ["project", "tag_name"],
        ),
        _tool("update_gitlab_release", "Change a GitLab release's title or notes.",
              {"project": project_p, "tag_name": tag_p, "name": _s("New title"), "description": _s("New notes")},
              ["project", "tag_name"]),
        _tool("delete_gitlab_release", "Delete a GitLab release (its tag stays).",
              {"project": project_p, "tag_name": tag_p}, ["project", "tag_name"]),
    ]
