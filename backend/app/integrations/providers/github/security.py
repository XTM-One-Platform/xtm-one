"""GitHub security tools - code scanning, Dependabot, secret scanning + repo advisory reads."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _BASE_URL, _PROVIDER, _json_headers, parse_repo

_SOURCE = "integration:github"

_REPO_ADVISORY_PERMISSION_HINT = (
    "grant the GitHub App or fine-grained token 'Repository security advisories: Read' "
    "and repository access; for an OAuth or classic token, grant 'repo' or "
    "'repository_advisories:read' and authorize the acting user for the advisory."
)
_REPO_ADVISORY_STATES = ("triage", "draft", "published", "closed")
_UNPUBLISHED_ADVISORY_NOTE = (
    "GitHub returns unpublished advisories (triage, draft, closed) only when the authorizing user is an "
    "administrator or security manager of the repository, or a collaborator on the advisory, and the "
    "credential can read them: a GitHub App needs 'Repository security advisories: Read' (set by the app "
    "owner under Developer settings > GitHub Apps > Permissions & events, then accepted on the installation), "
    "a classic token 'repo' or 'repository_advisories:read'. Otherwise they are silently omitted."
)
_REPO_ADVISORY_SORTS = ("created", "updated", "published")
_SORT_DIRECTIONS = ("asc", "desc")
_MAX_ADVISORY_ITEMS = 10
_MAX_ADVISORY_OUTPUT = 12_000


def _normalize_ghsa_id(ghsa_id: Any) -> tuple[str, str | None]:
    value = str(ghsa_id or "").strip()
    if not value:
        return value, "Error: 'ghsa_id' is required."
    if not re.fullmatch(r"GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}", value, flags=re.IGNORECASE):
        return value, (
            f"Error: invalid GHSA advisory id '{value}'. Use the GitHub Security Advisory id "
            "format such as 'GHSA-xxxx-xxxx-xxxx'."
        )
    return value.lower(), None


def _choice_error(name: str, value: str, choices: tuple[str, ...]) -> str | None:
    if value not in choices:
        return f"Error: invalid {name} '{value}'. Use one of: {', '.join(choices)}."
    return None


def _format_vulnerability(vulnerability: Any) -> str:
    if not isinstance(vulnerability, dict):
        return "unknown package"
    package = vulnerability.get("package") or {}
    if not isinstance(package, dict):
        package = {}
    name = package.get("name") or "unknown"
    ecosystem = package.get("ecosystem") or "unknown"
    affected = vulnerability.get("vulnerable_version_range") or "range unknown"
    patched = vulnerability.get("patched_versions")
    if not patched:
        first_patched = vulnerability.get("first_patched_version") or {}
        if isinstance(first_patched, dict):
            patched = first_patched.get("identifier")
    suffix = f"; patched: {patched}" if patched else ""
    return f"{name} / {ecosystem}: {affected}{suffix}"


_MAX_CURSOR_CHARS = 200


def _sanitize_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().replace("\r", " ").replace("\n", " ")
    # Rejected, never cut: a truncated cursor is a different cursor, and the
    # next call would silently page from the wrong place.
    if len(cleaned) > _MAX_CURSOR_CHARS:
        return None
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9._~=-]+", cleaned):
        return None
    return cleaned


def _next_page(response: httpx.Response) -> tuple[bool, str | None]:
    """``(has_next_link, cursor)`` from GitHub's ``Link`` header.

    The two are reported apart: a ``rel="next"`` link whose cursor fails
    :func:`_sanitize_cursor` (or carries no ``after``) still means more results
    exist, and must not read like a final page.
    """
    has_next = False
    for link in response.headers.get("Link", "").split(","):
        if 'rel="next"' not in link:
            continue
        has_next = True
        match = re.search(r"<([^>]+)>", link)
        if not match:
            continue
        values = parse_qs(urlparse(match.group(1)).query).get("after")
        if values and (cursor := _sanitize_cursor(values[0])):
            return True, cursor
    return has_next, None


_UNRENDERABLE_CURSOR_NOTICE = (
    "More results are available, but the pagination cursor could not be safely "
    "rendered. Narrow the query with 'state', 'sort' or 'direction' instead of "
    "assuming this list is complete."
)


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub security tools and register their callables."""

    async def list_github_code_scanning_alerts(
        repo: str, state: str = "open", severity: str = "", count: int = 20
    ) -> str:
        """List code scanning alerts for a repository.

        Filter by state (open, closed, dismissed, fixed) and severity
        (critical, high, medium, low, warning, note, error).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        params: dict[str, Any] = {"state": state, "per_page": max(1, min(count, 30))}
        if severity:
            params["severity"] = severity
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/code-scanning/alerts",
                _PROVIDER,
                f"list code scanning alerts in {repo}",
                headers=headers,
                params=params,
            )
            if err:
                if "not found" in err.lower():
                    return f"{err}\nHint: code scanning may not be enabled for {repo}."
                return err
            alerts = resp.json()
            if not alerts:
                return f"No {state} code scanning alerts in {repo}."
            lines = [f"Code scanning alerts in {repo} ({len(alerts)} {state}):"]
            for a in alerts:
                rule = a.get("rule", {})
                sev = rule.get("security_severity_level") or rule.get("severity") or "?"
                tool_name = (a.get("tool") or {}).get("name", "?")
                location = a.get("most_recent_instance", {}).get("location", {})
                path = location.get("path", "?")
                line_num = location.get("start_line", "?")
                lines.append(
                    f"  #{a['number']} [{sev.upper()}] {rule.get('description', 'No description')} "
                    f"({tool_name}) at {path}:{line_num} [{a.get('state', '?')}]"
                )
            return "\n".join(lines)

    async def list_github_dependabot_alerts(repo: str, state: str = "open", severity: str = "", count: int = 20) -> str:
        """List Dependabot vulnerability alerts for a repository.

        Filter by state (open, dismissed, fixed, auto_dismissed) and
        severity (critical, high, medium, low).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        params: dict[str, Any] = {"state": state, "per_page": max(1, min(count, 30))}
        if severity:
            params["severity"] = severity
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/dependabot/alerts",
                _PROVIDER,
                f"list Dependabot alerts in {repo}",
                headers=headers,
                params=params,
            )
            if err:
                if "not found" in err.lower():
                    return f"{err}\nHint: Dependabot alerts may not be enabled for {repo}."
                return err
            alerts = resp.json()
            if not alerts:
                return f"No {state} Dependabot alerts in {repo}."
            lines = [f"Dependabot alerts in {repo} ({len(alerts)} {state}):"]
            for a in alerts:
                vuln = a.get("security_advisory", {})
                sev = vuln.get("severity") or "?"
                pkg = a.get("dependency", {}).get("package", {})
                pkg_name = pkg.get("name", "?")
                ecosystem = pkg.get("ecosystem", "?")
                cve = vuln.get("cve_id") or "no CVE"
                lines.append(
                    f"  #{a['number']} [{sev.upper()}] {vuln.get('summary', 'No summary')} "
                    f"({pkg_name} / {ecosystem}) — {cve} [{a.get('state', '?')}]"
                )
            return "\n".join(lines)

    async def list_github_secret_scanning_alerts(repo: str, state: str = "open", count: int = 20) -> str:
        """List secret scanning alerts (leaked credentials/tokens detected in the repo).

        Filter by state (open, resolved).
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        params: dict[str, Any] = {"per_page": max(1, min(count, 30))}
        if state in ("open", "resolved"):
            params["state"] = state
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/secret-scanning/alerts",
                _PROVIDER,
                f"list secret scanning alerts in {repo}",
                headers=headers,
                params=params,
            )
            if err:
                if "not found" in err.lower():
                    return f"{err}\nHint: secret scanning may not be enabled for {repo}."
                return err
            alerts = resp.json()
            if not alerts:
                return f"No {state} secret scanning alerts in {repo}."
            lines = [f"Secret scanning alerts in {repo} ({len(alerts)} {state}):"]
            for a in alerts:
                resolution = f" — {a.get('resolution')}" if a.get("resolution") else ""
                lines.append(
                    f"  #{a.get('number')} {a.get('secret_type_display_name') or a.get('secret_type', '?')} "
                    f"[{a.get('state', '?')}{resolution}] — {a.get('html_url', '')}"
                )
            return "\n".join(lines)

    async def list_github_repository_security_advisories(
        repo: str,
        state: str = "",
        sort: str = "updated",
        direction: str = "desc",
        after: str = "",
        count: int = 20,
    ) -> str:
        """List repository security advisories for a repository.

        Read-only access to GitHub's repository security advisory feed for the
        selected repository. `state` is the repository advisory lifecycle state:

        `triage`, `draft`, `published`, or `closed`; empty lists every state.
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        state = (state or "").strip().lower()
        for field, value, choices in (
            ("state", state, _REPO_ADVISORY_STATES),
            ("sort", sort, _REPO_ADVISORY_SORTS),
            ("direction", direction, _SORT_DIRECTIONS),
        ):
            if field == "state" and not value:
                continue
            if error := _choice_error(field, value, choices):
                return error
        if after and not state:
            return "Error: an 'after' cursor belongs to one lifecycle state; pass the 'state' it was returned for."
        per_page = max(1, min(count, 30))

        async def fetch(client: httpx.AsyncClient, one_state: str) -> tuple[Any, str | None]:
            params: dict[str, Any] = {"state": one_state, "sort": sort, "direction": direction, "per_page": per_page}
            if after:
                params["after"] = after
            return await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/security-advisories",
                _PROVIDER,
                f"list {one_state} repository security advisories in {repo}",
                headers=headers,
                params=params,
            )

        async with egress_client(timeout=30) as client:
            if state:
                resp, err = await fetch(client, state)
                if err:
                    return _common.with_permission_hint(err, _REPO_ADVISORY_PERMISSION_HINT)
                advisories = resp.json()
                if not advisories:
                    empty = f"No {state} repository security advisories in {repo}."
                    return empty if state == "published" else f"{empty} {_UNPUBLISHED_ADVISORY_NOTE}"
                lines = [f"Repository security advisories in {repo} ({len(advisories)} {state} on this page):"]
                has_next, cursor = _next_page(resp)
                if cursor:
                    lines.append(f"More results are available; call this tool again with after={cursor}.")
                elif has_next:
                    lines.append(_UNRENDERABLE_CURSOR_NOTICE)
            else:
                # GitHub filters one state per request, so "every state" is one request per state.
                results = await asyncio.gather(*(fetch(client, s) for s in _REPO_ADVISORY_STATES))
                failed = [(s, err) for s, (_, err) in zip(_REPO_ADVISORY_STATES, results, strict=True) if err]
                if len(failed) == len(_REPO_ADVISORY_STATES):
                    return _common.with_permission_hint(failed[0][1], _REPO_ADVISORY_PERMISSION_HINT)
                seen: set[str] = set()
                merged: list[tuple[str, dict]] = []
                next_pages: dict[str, str | None] = {}
                for one_state, (resp, err) in zip(_REPO_ADVISORY_STATES, results, strict=True):
                    if err:
                        continue
                    page = resp.json()
                    for advisory in page if isinstance(page, list) else []:
                        if not isinstance(advisory, dict):
                            continue
                        ghsa = str(advisory.get("ghsa_id") or "").lower()
                        if ghsa and ghsa in seen:
                            continue
                        if ghsa:
                            seen.add(ghsa)
                        merged.append((one_state, advisory))
                    has_next, cursor = _next_page(resp)
                    if has_next:
                        next_pages[one_state] = cursor
                sort_key = f"{sort}_at"
                dated = sorted(
                    (item for item in merged if item[1].get(sort_key)),
                    key=lambda item: item[1][sort_key],
                    reverse=direction == "desc",
                )
                ordered = dated + [item for item in merged if not item[1].get(sort_key)]
                # A state with fetched advisories left off this page restarts at its page one: its cursor would skip them.
                cut_states = {one_state for one_state, _ in ordered[per_page:]}
                advisories = [advisory for _, advisory in ordered[:per_page]]
                failure_lines = [
                    f"Could not list {s} advisories: "
                    f"{_common.with_permission_hint(err, _REPO_ADVISORY_PERMISSION_HINT)}"
                    for s, err in failed
                ]
                if not advisories:
                    return "\n".join(
                        [f"No repository security advisories in {repo}. {_UNPUBLISHED_ADVISORY_NOTE}", *failure_lines]
                    )
                lines = [f"Repository security advisories in {repo} ({len(advisories)} across every state):"]
                lines.extend(failure_lines)
                if all(s == "published" for s, _ in merged):
                    lines.append(f"Only published advisories were returned. {_UNPUBLISHED_ADVISORY_NOTE}")
                for one_state in _REPO_ADVISORY_STATES:
                    if one_state in cut_states:
                        lines.append(
                            f"More {one_state} advisories exist; call this tool again with state={one_state} "
                            "to page through them."
                        )
                    elif one_state in next_pages:
                        cursor = next_pages[one_state]
                        if cursor:
                            lines.append(
                                f"More {one_state} advisories exist; call this tool again with "
                                f"state={one_state}, after={cursor} and the same sort, direction and count."
                            )
                        else:
                            lines.append(
                                f"More {one_state} advisories exist, but the pagination cursor could not be safely "
                                "rendered; narrow the query with 'sort' or 'direction' instead of assuming this "
                                "list is complete."
                            )
            for advisory in advisories:
                ghsa = advisory.get("ghsa_id") or advisory.get("cve_id") or "?"
                severity = (advisory.get("severity") or "unknown").upper()
                summary = _common._truncate(
                    advisory.get("summary") or advisory.get("description") or "No summary",
                    500,
                )
                cve = advisory.get("cve_id") or "no CVE"
                updated = advisory.get("updated_at") or advisory.get("published_at") or "unknown"
                vulnerabilities = advisory.get("vulnerabilities") or []
                affected = "; ".join(_format_vulnerability(item) for item in vulnerabilities[:_MAX_ADVISORY_ITEMS])
                lines.append(
                    f"  {ghsa} [{severity}] {summary} — {cve} [{advisory.get('state') or 'unknown'}]\n"
                    f"    Affected: {affected or 'none listed'}\n"
                    f"    Updated: {updated} | URL: {advisory.get('html_url') or 'n/a'}"
                )
            return _common._truncate("\n".join(lines), _MAX_ADVISORY_OUTPUT)

    async def get_github_repository_security_advisory(repo: str, ghsa_id: str) -> str:
        """Get one repository security advisory by GHSA identifier."""
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        ghsa_id, gerr = _normalize_ghsa_id(ghsa_id)
        if gerr:
            return gerr
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{_BASE_URL}/repos/{owner}/{name}/security-advisories/{ghsa_id}",
                _PROVIDER,
                f"get security advisory {ghsa_id} in {repo}",
                headers=headers,
            )
            if err:
                return _common.with_permission_hint(err, _REPO_ADVISORY_PERMISSION_HINT)
            advisory = resp.json()
            state = advisory.get("state") or "unknown"
            severity = (advisory.get("severity") or "unknown").upper()
            summary = _common._truncate(advisory.get("summary") or advisory.get("description") or "No summary", 500)
            desc = advisory.get("description") or "No description"
            cve = advisory.get("cve_id") or "no CVE"
            references = advisory.get("references") or []
            refs = _common._truncate(
                " | ".join(
                    ref.get("url", "")
                    for ref in references[:_MAX_ADVISORY_ITEMS]
                    if isinstance(ref, dict) and ref.get("url")
                ),
                1000,
            )
            identifiers = advisory.get("identifiers") or []
            identifier_text = _common._truncate(
                ", ".join(
                    f"{item.get('type')}: {item.get('value')}"
                    for item in identifiers[:_MAX_ADVISORY_ITEMS]
                    if isinstance(item, dict) and item.get("type") and item.get("value")
                ),
                500,
            )
            vulnerabilities = advisory.get("vulnerabilities") or []
            affected = _common._truncate(
                "; ".join(_format_vulnerability(item) for item in vulnerabilities[:_MAX_ADVISORY_ITEMS]),
                1500,
            )
            cvss = advisory.get("cvss_severities") or {}
            if not isinstance(cvss, dict):
                cvss = {}
            cvss_text = ", ".join(
                f"CVSS {version.removeprefix('cvss_')}: {details.get('score')} "
                f"({details.get('vector_string') or 'no vector'})"
                for version, details in cvss.items()
                if isinstance(details, dict) and details.get("score") is not None
            )
            cwes = advisory.get("cwes") or []
            cwe_text = _common._truncate(
                ", ".join(
                    f"{item.get('cwe_id')}: {item.get('name')}"
                    for item in cwes[:_MAX_ADVISORY_ITEMS]
                    if isinstance(item, dict) and item.get("cwe_id")
                ),
                500,
            )
            credits = advisory.get("credits") or []
            credit_text = _common._truncate(
                ", ".join(
                    str(item.get("login"))
                    for item in credits[:_MAX_ADVISORY_ITEMS]
                    if isinstance(item, dict) and item.get("login")
                ),
                500,
            )
            lines = [
                f"Repository security advisory {ghsa_id} in {repo}:",
                f"  Summary: {summary}",
                f"  Severity: {severity}",
                f"  State: {state}",
                f"  CVE: {cve}",
                f"  Identifiers: {identifier_text or ghsa_id}",
                f"  CVSS: {cvss_text or 'not provided'}",
                f"  CWEs: {cwe_text or 'not provided'}",
                f"  Affected: {affected or 'none listed'}",
                f"  Credits: {credit_text or 'none listed'}",
                f"  Created: {advisory.get('created_at') or 'n/a'}",
                f"  Updated: {advisory.get('updated_at') or 'n/a'}",
                f"  Published: {advisory.get('published_at') or 'n/a'}",
                f"  Closed: {advisory.get('closed_at') or 'n/a'}",
                f"  Withdrawn: {advisory.get('withdrawn_at') or 'n/a'}",
                f"  URL: {advisory.get('html_url') or 'n/a'}",
                f"  References: {refs or 'none'}",
                f"  Description: {_common._truncate(desc, 3000)}",
            ]
            return _common._truncate("\n".join(lines), _MAX_ADVISORY_OUTPUT)

    async def manage_github_alert(
        repo: str,
        alert_type: str,
        alert_number: int,
        state: str,
        reason: str = "",
        comment: str = "",
    ) -> str:
        """Dismiss/resolve or reopen a security alert (write).

        ``alert_type``: ``code_scanning`` | ``dependabot`` | ``secret_scanning``.
        ``state``: ``dismiss`` (close it) or ``open`` (reopen). ``reason`` is the
        type-appropriate dismissal/resolution reason:
          - code_scanning: ``false positive`` | ``won't fix`` | ``used in tests``
          - dependabot: ``fix_started`` | ``inaccurate`` | ``no_bandwidth`` |
            ``not_used`` | ``tolerable_risk``
          - secret_scanning: ``false_positive`` | ``wont_fix`` | ``revoked`` |
            ``used_in_tests``
        """
        owner, name, rerr = parse_repo(repo)
        if rerr:
            return rerr
        alert_type = (alert_type or "").strip().lower()
        close = (state or "").strip().lower() in ("dismiss", "dismissed", "resolve", "resolved", "close", "closed")
        payload: dict[str, Any] = {}
        if alert_type == "code_scanning":
            url = f"{_BASE_URL}/repos/{owner}/{name}/code-scanning/alerts/{alert_number}"
            if close:
                payload["state"] = "dismissed"
                payload["dismissed_reason"] = reason or "won't fix"
                if comment:
                    payload["dismissed_comment"] = comment
            else:
                payload["state"] = "open"
        elif alert_type == "dependabot":
            url = f"{_BASE_URL}/repos/{owner}/{name}/dependabot/alerts/{alert_number}"
            if close:
                payload["state"] = "dismissed"
                payload["dismissed_reason"] = reason or "tolerable_risk"
                if comment:
                    payload["dismissed_comment"] = comment
            else:
                payload["state"] = "open"
        elif alert_type == "secret_scanning":
            url = f"{_BASE_URL}/repos/{owner}/{name}/secret-scanning/alerts/{alert_number}"
            if close:
                payload["state"] = "resolved"
                payload["resolution"] = reason or "wont_fix"
                if comment:
                    payload["resolution_comment"] = comment
            else:
                payload["state"] = "open"
        else:
            return "Error: invalid alert_type. Use code_scanning, dependabot, or secret_scanning."
        async with egress_client(timeout=30) as client:
            _resp, err = await _common.safe_request(
                client,
                "patch",
                url,
                _PROVIDER,
                f"update {alert_type} alert #{alert_number} in {repo}",
                headers=_json_headers(headers),
                json=payload,
            )
            if err:
                return err
            verb = "dismissed/resolved" if close else "reopened"
            return f"{alert_type.replace('_', ' ').title()} alert #{alert_number} {verb} in {owner}/{name}."

    executor.register("list_github_code_scanning_alerts", list_github_code_scanning_alerts)
    executor.register("list_github_dependabot_alerts", list_github_dependabot_alerts)
    executor.register("list_github_secret_scanning_alerts", list_github_secret_scanning_alerts)
    executor.register("list_github_repository_security_advisories", list_github_repository_security_advisories)
    executor.register("get_github_repository_security_advisory", get_github_repository_security_advisory)
    executor.register("manage_github_alert", manage_github_alert)

    _repo = {"type": "string", "description": "Repository in format owner/name"}

    return [
        ToolDefinition(
            name="list_github_code_scanning_alerts",
            description=(
                "List code scanning (SAST) alerts for a repository. Shows severity, rule description, tool name, and "
                "file location. Filter by state (open, closed, dismissed, fixed) and severity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {"type": "string", "enum": ["open", "closed", "dismissed", "fixed"], "default": "open"},
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity: critical, high, medium, low, warning, note, error (optional)",
                        "default": "",
                    },
                    "count": {"type": "integer", "description": "Number of alerts (max 30)", "default": 20},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_dependabot_alerts",
            description=(
                "List Dependabot vulnerability alerts for a repository. Shows severity, advisory summary, affected "
                "package/ecosystem, and CVE ID. Filter by state (open, dismissed, fixed, auto_dismissed) and severity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {
                        "type": "string",
                        "enum": ["open", "dismissed", "fixed", "auto_dismissed"],
                        "default": "open",
                    },
                    "severity": {
                        "type": "string",
                        "description": "Filter by severity: critical, high, medium, low (optional)",
                        "default": "",
                    },
                    "count": {"type": "integer", "description": "Number of alerts (max 30)", "default": 20},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_secret_scanning_alerts",
            description=(
                "List secret scanning alerts - leaked credentials/tokens detected in the repository. Shows the secret "
                "type, state, and resolution. Filter by state (open, resolved)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {"type": "string", "enum": ["open", "resolved"], "default": "open"},
                    "count": {"type": "integer", "description": "Number of alerts (max 30)", "default": 20},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_github_repository_security_advisories",
            description=(
                "List repository security advisories with affected packages and versions. Filter by lifecycle state, "
                "sort order, and pagination cursor; pass the returned after cursor to continue listing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "state": {
                        "type": "string",
                        "enum": list(_REPO_ADVISORY_STATES),
                        "description": "Lifecycle state to filter on. Omit to list every state.",
                    },
                    "sort": {"type": "string", "enum": list(_REPO_ADVISORY_SORTS), "default": "updated"},
                    "direction": {"type": "string", "enum": list(_SORT_DIRECTIONS), "default": "desc"},
                    "after": {
                        "type": "string",
                        "description": "Pagination cursor from a previous result, with the same state",
                        "default": "",
                    },
                    "count": {"type": "integer", "description": "Number of advisories (max 30)", "default": 20},
                },
                "required": ["repo"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_github_repository_security_advisory",
            description=(
                "Fetch a repository security advisory by GHSA ID, including severity, affected and patched versions, "
                "CVSS, CWEs, credits, lifecycle timestamps, references, and a bounded description."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "ghsa_id": {"type": "string", "description": "GHSA advisory id, e.g. GHSA-xxxx-xxxx-xxxx"},
                },
                "required": ["repo", "ghsa_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_github_alert",
            description=(
                "Dismiss/resolve or reopen a security alert. alert_type=code_scanning|dependabot|secret_scanning; "
                "state=dismiss (close) or open (reopen); reason is the type-appropriate dismissal reason "
                "(code_scanning: false positive|won't fix|used in tests; dependabot: fix_started|inaccurate|no_bandwidth|"
                "not_used|tolerable_risk; secret_scanning: false_positive|wont_fix|revoked|used_in_tests). "
                "Triage findings without leaving a finding open."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": _repo,
                    "alert_type": {"type": "string", "enum": ["code_scanning", "dependabot", "secret_scanning"]},
                    "alert_number": {"type": "integer", "description": "The alert number"},
                    "state": {"type": "string", "enum": ["dismiss", "open"]},
                    "reason": {
                        "type": "string",
                        "description": "Type-appropriate dismissal/resolution reason (see description)",
                        "default": "",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional dismissal/resolution comment",
                        "default": "",
                    },
                },
                "required": ["repo", "alert_type", "alert_number", "state"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
