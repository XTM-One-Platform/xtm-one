"""Shared helpers for the Atlassian Jira integration package.

Holds the constants, credential parsing, HTTP wrapper, Atlassian Document
Format (ADF) conversion, and the issue / custom-field formatters used by every
Jira domain module. Mirrors the ``opencti/_common.py`` split.

Token format passed by the dispatcher: ``cloud_id||access_token`` (compound,
split by ``||``). The Atlassian ``cloud_id`` is prepended by ``_get_token`` in
``integration_tools.py`` from the integration's ``extra_config``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    AutoRefreshClient,
    bearer_auth_headers,
    parse_compound_token,
    safe_request,
)

logger = logging.getLogger(__name__)

_PROVIDER = "Jira"
_SOURCE = "integration:jira"
_BASE_URL = "https://api.atlassian.com"

# Truncate large ADF descriptions / comment bodies for LLM context.
_MAX_BODY_CHARS = 4_000
# Per-field cap when rendering arbitrary custom-field values.
_MAX_FIELD_CHARS = 500
# A single comment read on purpose (get_jira_comment): the whole body, bounded
# only against a pathological payload.
_MAX_FULL_COMMENT_CHARS = 50_000
# Jira Cloud caps most list endpoints at 50/100; keep a single ceiling.
_MAX_PAGE = 100


# ---------------------------------------------------------------------------
# Credentials + URL builders
# ---------------------------------------------------------------------------


def parse_jira_token(compound: str) -> tuple[str, str]:
    """Split ``cloud_id||access_token`` into ``(site_base_url, access_token)``.

    ``site_base_url`` is ``https://api.atlassian.com/ex/jira/{cloud_id}`` with
    whitespace stripped from both halves so the bearer header and URLs never
    carry stray whitespace/control characters (which httpx rejects). Returns an
    empty ``site_base_url`` when no cloud_id is present so the caller degrades
    gracefully instead of issuing requests against an invalid site.
    """
    cloud_id, token = parse_compound_token(compound, 2)
    cloud_id = (cloud_id or "").strip()
    token = (token or "").strip()
    if cloud_id:
        return f"{_BASE_URL}/ex/jira/{cloud_id}", token
    return "", token


def api3(base: str) -> str:
    """Jira Platform REST API v3 root."""
    return f"{base}/rest/api/3"


def agile(base: str) -> str:
    """Jira Software (Agile) REST API root."""
    return f"{base}/rest/agile/1.0"


def jsm(base: str) -> str:
    """Jira Service Management REST API root."""
    return f"{base}/rest/servicedeskapi"


def jira_headers(token: str) -> dict[str, str]:
    """Build the standard ``Authorization: Bearer`` + ``Accept`` header dict.

    Uses :func:`bearer_auth_headers`, which guards against blank tokens so an
    empty credential surfaces as a clear error instead of an illegal
    ``Authorization: Bearer`` header.
    """
    return bearer_auth_headers(token, extra={"Accept": "application/json"})


class RefreshableHeaders(dict):
    """Header dict that also carries the dispatcher-supplied OAuth refresh hook.

    Atlassian access tokens expire after ~1 hour. The domain modules all pass
    the same shared ``headers`` dict into :func:`jira_request`, so attaching
    the refresh callback to that dict lets the HTTP wrapper route the call
    through :class:`AutoRefreshClient` — which refreshes on 401/403, retries
    once, and mutates this shared dict in place so every subsequent tool call
    picks up the fresh token — without threading an extra argument through
    every register()/tool signature.
    """

    refresh_fn: Any = None


def make_refreshable_headers(
    headers: dict[str, str], refresh_fn: Any
) -> RefreshableHeaders:
    """Wrap *headers* so :func:`jira_request` can auto-refresh on 401/403."""
    wrapped = RefreshableHeaders(headers)
    wrapped.refresh_fn = refresh_fn
    return wrapped


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------


async def jira_request(
    method: str,
    url: str,
    action: str,
    *,
    timeout: float = 30.0,
    expected_statuses: set[int] | None = None,
    follow_redirects: bool = False,
    **kwargs: Any,
) -> tuple[httpx.Response | None, str | None]:
    """Execute a Jira HTTP request via the shared ``safe_request`` helper.

    Opens a short-lived async client (the integration tool surface is mostly
    one request per call), applies standardised error handling + 429 retry, and
    returns ``(response, None)`` on success or ``(None, error_string)`` on
    failure (or ``(response, error_string)`` when ``expected_statuses`` is set,
    so callers can branch on a benign 404/403).

    When the caller's ``headers`` is a :class:`RefreshableHeaders` (built by
    ``build_tools`` with the dispatcher's ``refresh_fn``), the request routes
    through :class:`AutoRefreshClient` so an expired Atlassian access token is
    refreshed and the request retried instead of surfacing "re-authorize".
    """
    refresh_fn = getattr(kwargs.get("headers"), "refresh_fn", None)
    if refresh_fn is not None:
        if follow_redirects:
            kwargs["follow_redirects"] = True
        async with AutoRefreshClient(
            kwargs["headers"], refresh_fn, timeout=int(timeout)
        ) as client:
            return await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                expected_statuses=expected_statuses,
                **kwargs,
            )
    async with egress_client(
        timeout=timeout, follow_redirects=follow_redirects
    ) as client:
        return await safe_request(
            client,
            method,
            url,
            _PROVIDER,
            action,
            expected_statuses=expected_statuses,
            **kwargs,
        )


def clamp(count: Any, default: int, ceiling: int = _MAX_PAGE) -> int:
    """Coerce a model-supplied count into a sane positive int <= ceiling."""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, ceiling)


# ---------------------------------------------------------------------------
# Atlassian Document Format (ADF) <-> plain text
# ---------------------------------------------------------------------------


def text_to_adf(text: str) -> dict[str, Any]:
    """Wrap a plain-text string in a minimal Atlassian Document Format doc.

    Each line becomes a paragraph so line breaks survive. Used by every tool
    that writes a rich-text field (description, comment body, worklog comment).
    """
    content: list[dict[str, Any]] = []
    for line in (text or "").split("\n"):
        node: dict[str, Any] = {"type": "paragraph", "content": []}
        if line:
            node["content"].append({"type": "text", "text": line})
        content.append(node)
    if not content:
        content = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": content}


_ADF_BLOCK_TYPES = {
    "paragraph",
    "heading",
    "blockquote",
    "listItem",
    "codeBlock",
    "rule",
    "tableRow",
}


def adf_to_text(node: Any) -> str:
    """Recursively extract readable plain text from an ADF tree.

    Block-level nodes (paragraphs, headings, list items, ...) are separated by
    newlines and ``hardBreak`` nodes become line breaks, so the rendered text
    keeps a reasonable shape for the LLM. Plain strings pass through unchanged.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return str(node)
    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        attrs = node.get("attrs") or {}
        return attrs.get("text") or f"@{attrs.get('id', '')}"
    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if node_type in _ADF_BLOCK_TYPES:
        return inner + "\n"
    return inner


def body_text(body: Any) -> str:
    """Normalise a Jira body (plain string or ADF dict) to stripped text."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        return adf_to_text(body).strip()
    return str(body).strip()


def truncate(text: str, limit: int = _MAX_FIELD_CHARS) -> str:
    """Trim *text* to *limit* characters with an ellipsis marker."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def truncate_body(text: str, limit: int = _MAX_BODY_CHARS, *, read_more: str = "") -> str:
    """Trim a long body, saying how much was cut and how to read the rest.

    A bare ``...`` reads as the end of the text; an agent summarizing a ticket
    must know the comment continues.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    hint = f"; {read_more}" if read_more else ""
    return f"{text[:limit]}... [truncated: {limit:,} of {len(text):,} characters{hint}]"


def comment_body(comment: dict[str, Any], issue_key: str) -> str:
    """A comment body up to the body cap, pointing at ``get_jira_comment`` past it."""
    return truncate_body(
        body_text(comment.get("body")),
        read_more=(
            f"read it in full with get_jira_comment(issue_key='{issue_key}', "
            f"comment_id='{comment.get('id', '?')}')"
        ),
    )


# ---------------------------------------------------------------------------
# Field value formatting
# ---------------------------------------------------------------------------


def format_field_value(value: Any) -> str:
    """Render an arbitrary Jira field value (system or custom) as readable text.

    Handles the common Jira shapes: scalars, option objects (``{"value": ...}``),
    named objects (priority/status/resolution ``{"name": ...}``), user objects
    (``{"displayName": ...}``), ADF rich text (``{"type": "doc", ...}``), and
    lists of any of those. Falls back to a compact ``str`` for unknown shapes.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        rendered = [format_field_value(item) for item in value]
        return ", ".join(part for part in rendered if part)
    if isinstance(value, dict):
        if value.get("type") == "doc":  # ADF rich-text field
            return body_text(value)
        for key in (
            "displayName",
            "name",
            "value",
            "emailAddress",
            "key",
            "label",
            "text",
            "id",
        ):
            if value.get(key):
                return str(value[key])
        # Unknown object shape (e.g. a custom field whose value is an object
        # with none of the keys above): fall back to a compact str rather than
        # silently dropping the value, which would hide real custom-field data.
        return str(value)
    return str(value)


def _format_custom_fields(
    fields: dict[str, Any], names: dict[str, str] | None
) -> list[str]:
    """Render every populated ``customfield_*`` value as ``Label: value`` lines.

    This is what makes custom fields (e.g. a "Github url" field) actually
    surface to the agent. ``names`` maps field IDs to human labels and is
    present when the issue was fetched with ``expand=names``; without it the
    field ID is shown so the agent can still cross-reference it.
    """
    lines: list[str] = []
    for key, raw in fields.items():
        if not key.startswith("customfield_"):
            continue
        if raw is None or raw == [] or raw == "":
            continue
        rendered = truncate(format_field_value(raw))
        if not rendered:
            continue
        label = (names or {}).get(key, key)
        lines.append(f"  {label}: {rendered}")
    return lines


def format_attachment(a: dict[str, Any]) -> str:
    """One line for a Jira attachment: name, size, type, id, author, date, content URL."""
    size_kb = round((a.get("size") or 0) / 1024, 1)
    author = (a.get("author") or {}).get("displayName", "?")
    line = (
        f"{a.get('filename', '?')} ({size_kb} KB, {a.get('mimeType', '?')}) "
        f"- id: {a.get('id', '?')}, by {author} on {(a.get('created') or '?')[:10]}"
    )
    if a.get("content"):
        line += f" - download: {a['content']}"
    return line


def format_issue(issue: dict[str, Any], *, detail: bool = False) -> str:
    """Format a Jira issue dict into a readable string.

    When ``detail`` is set, includes description, resolution, parent, time
    tracking and (crucially) every populated custom field. The custom-field
    labels are resolved from the issue's ``names`` map when it was fetched with
    ``expand=names``.
    """
    fields = issue.get("fields") or {}
    names = issue.get("names") or None
    key = issue.get("key", "?")
    summary = fields.get("summary", "No summary")
    status = (fields.get("status") or {}).get("name", "?")
    issue_type = (fields.get("issuetype") or {}).get("name", "?")
    priority = (fields.get("priority") or {}).get("name", "?")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    reporter = (fields.get("reporter") or {}).get("displayName", "?")

    lines = [f"{key}: {summary}"]
    lines.append(f"  Type: {issue_type} | Status: {status} | Priority: {priority}")
    lines.append(f"  Assignee: {assignee} | Reporter: {reporter}")

    labels = fields.get("labels")
    if labels:
        lines.append(f"  Labels: {', '.join(labels)}")

    components = fields.get("components")
    if components:
        lines.append(f"  Components: {', '.join(c.get('name', '') for c in components)}")

    fix_versions = fields.get("fixVersions")
    if fix_versions:
        lines.append(
            f"  Fix Versions: {', '.join(v.get('name', '') for v in fix_versions)}"
        )

    if detail:
        lines.append(
            f"  Created: {fields.get('created', '?')} | Updated: {fields.get('updated', '?')}"
        )
        due = fields.get("duedate")
        if due:
            lines.append(f"  Due: {due}")
        resolution = (fields.get("resolution") or {}).get("name")
        if resolution:
            lines.append(f"  Resolution: {resolution}")
        parent = fields.get("parent")
        if parent:
            lines.append(
                f"  Parent: {parent.get('key', '?')} - "
                f"{(parent.get('fields') or {}).get('summary', '?')}"
            )
        desc = fields.get("description")
        if desc:
            lines.append(f"\n  Description: {truncate(body_text(desc), _MAX_BODY_CHARS)}")
        attachments = [a for a in fields.get("attachment") or [] if isinstance(a, dict)]
        if attachments:
            lines.append(f"  Attachments ({len(attachments)}):")
            lines.extend(f"    {format_attachment(a)}" for a in attachments)
        # Custom fields - the whole point of the deep dive.
        custom_lines = _format_custom_fields(fields, names)
        if custom_lines:
            lines.append("  Custom fields:")
            lines.extend(custom_lines)
    else:
        # In list/search context, still surface any custom fields the caller
        # explicitly requested (they will not be present otherwise).
        custom_lines = _format_custom_fields(fields, names)
        lines.extend(custom_lines)

    return "\n".join(lines)
