# SPDX-License-Identifier: Apache-2.0

"""Google Docs review: comment threads, anchored comments, suggestions, edits.

The tools a reviewer needs on a Google Doc, registered by ``google.build_tools``:

* ``list_google_doc_comments`` - every thread (Drive ``comments.list``): its id,
  author, the text it is attached to, its content, status and replies.
* ``add_google_doc_comment`` - a comment under the connected user's name. With
  ``quoted_text`` it is pinned to that passage (Docs ``insertComment`` on the
  range of a quote found exactly once in the body, so the editor highlights it
  like a comment made by hand); without, it is on the file (Drive
  ``comments.create``).
* ``reply_google_doc_comment`` / ``resolve_google_doc_comment`` - a reply that
  may resolve or reopen the thread (Drive ``replies.create``).
* ``edit_google_doc`` - find-and-replace (Docs ``replaceAllText``), written as
  suggestions unless a direct edit is asked for. Every find is counted on the
  current text before anything is applied, and a direct edit is pinned to the
  revision it was counted on.

``read_google_doc`` takes a Doc's text from :meth:`GoogleDocTools.document_text`
and its threads from :meth:`GoogleDocTools.comments_section`.

Every call acts as the connected user. A scope the token lacks is named before
the request that needs it (Google's ``tokeninfo`` lists the granted scopes),
and a 403 / 404 / 429 becomes a sentence naming the file.
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as _url_encode

import httpx

from app.integrations.file_content import MAX_DOCUMENT_CHARS, cut_inline
from app.integrations.http import AutoRefreshClient, safe_request
from app.integrations.passage_match import near_matches
from app.tools.base import ToolDefinition

_PROVIDER = "Google"
_SOURCE = "integration:google"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_DOCS_API = "https://docs.googleapis.com/v1"
_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
DOCS_READONLY_SCOPE = "https://www.googleapis.com/auth/documents.readonly"

#: Google's limit on the text of a comment created through the Docs API.
_ANCHORED_COMMENT_MAX_BYTES = 2048
_COMMENT_PAGE_SIZE = 100
_MAX_COMMENT_PAGES = 20
_MAX_EDITS = 50
_MAX_CONTEXTS = 5
_READ_MAX_THREADS = 50
#: A placeholder for what is not text (an image, a chip, a table boundary):
#: a quote or a find never matches across it, as in the Docs editor.
_OBJECT = "\ufffc"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_URL_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
_URL_QUERY_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
_WHEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")

_SCOPE_REASONS = frozenset({"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"})
_RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"}
)
_DISABLED_REASONS = frozenset({"SERVICE_DISABLED", "accessNotConfigured"})


@dataclass(frozen=True)
class ScopeNeed:
    """The scope a call needs: *named* in the error, any of *accepted* will do."""

    named: str
    accepted: frozenset[str]


#: ``files.get``, ``comments.list``.
_READ_DRIVE = ScopeNeed(DRIVE_SCOPE, frozenset({DRIVE_SCOPE, DRIVE_READONLY_SCOPE}))
#: ``documents.get``.
_READ_DOC = ScopeNeed(
    DOCS_SCOPE,
    frozenset({DOCS_SCOPE, DOCS_READONLY_SCOPE, DRIVE_SCOPE, DRIVE_READONLY_SCOPE}),
)
#: ``comments.create`` / ``replies.create`` on a file the app did not create:
#: ``drive.file`` only reaches the files the app created or was handed.
_COMMENT = ScopeNeed(DRIVE_SCOPE, frozenset({DRIVE_SCOPE}))
#: ``documents.batchUpdate``: an anchored comment, a suggestion, an edit.
_WRITE_DOC = ScopeNeed(DOCS_SCOPE, frozenset({DOCS_SCOPE, DRIVE_SCOPE}))


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_file_id(raw: Any) -> str | None:
    """The Drive file id in *raw*: an id, or a Docs / Drive URL carrying one."""
    value = str(raw or "").strip()
    if "/" in value:
        match = _URL_ID_RE.search(value) or _URL_QUERY_ID_RE.search(value)
        return match.group(1) if match else None
    return value if _ID_RE.fullmatch(value) else None


def bool_arg(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _path(value: str) -> str:
    return _url_encode(value, safe="")


def _short(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _label(meta: dict | None, file_id: str) -> str:
    name = (meta or {}).get("name")
    return f"'{name}'" if name else f"file {file_id}"


def _link(meta: dict | None) -> str:
    url = (meta or {}).get("webViewLink")
    return f"\nWeb URL: {url}" if url else ""


# ---------------------------------------------------------------------------
# Errors: a sentence naming the file, never a raw 403
# ---------------------------------------------------------------------------


def scope_error(scope: str, action: str, label: str) -> str:
    """The answer when the integration's token lacks *scope*."""
    return (
        f"Error: the Google integration is not authorized to {action} {label}: its token "
        f"lacks the OAuth scope {scope}. Open the integration on the Integrations page and "
        "click 'Re-authorize with Google' so Google grants the missing permission, then "
        "try again."
    )


def _as_note(error: str) -> str:
    """An ``Error: ...`` answer as a sentence inside a successful one."""
    detail = error.removeprefix("Error: ")
    return detail[:1].upper() + detail[1:]


def export_note(reason: str) -> str:
    """What ``read_google_doc`` says above Drive's export of a Doc the Docs API did not read."""
    return (
        "(This is Drive's plain-text export, where [a], [b], ... mark comments: the Google "
        f"Docs API did not read this Doc. {_as_note(reason)})"
    )


def _error_body(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _error_reasons(resp: httpx.Response) -> set[str]:
    """The machine-readable reasons of a Google error (Drive v3 and v1 shapes)."""
    error = _error_body(resp)
    reasons = {str(error.get("status") or "")}
    for key in ("errors", "details"):
        for item in error.get(key) or []:
            if isinstance(item, dict):
                reasons.add(str(item.get("reason") or ""))
    reasons.discard("")
    return reasons


def _error_message(resp: httpx.Response | None) -> str:
    return str(_error_body(resp).get("message") or "") if resp is not None else ""


_API_NAMES = {"docs.googleapis.com": "Google Docs API", "sheets.googleapis.com": "Google Sheets API"}


def _api_name(resp: httpx.Response) -> str:
    try:
        host = resp.request.url.host
    except RuntimeError:
        host = ""
    return _API_NAMES.get(host, "Google Drive API")


def failure(
    resp: httpx.Response | None,
    err: str | None,
    *,
    action: str,
    label: str,
    need: ScopeNeed,
    role: str,
    not_found: str | None = None,
) -> str:
    """A failed Google call as a sentence: what failed, on which file, what to do.

    *role* is the access the call needs (``commenter``, ``editor``): a refusal
    asks the file's owner for it.
    """
    if resp is None:
        return err or f"Error: Google could not {action} {label}."
    status = resp.status_code
    reasons = _error_reasons(resp)
    if status == 404:
        return not_found or (
            f"Error: Google Drive cannot find {label}: it does not exist, was deleted, or "
            "is not shared with the connected Google account. Check the file ID or link."
        )
    if status == 429 or reasons & _RATE_REASONS:
        return (
            f"Error: Google is throttling this account's requests and refused to {action} "
            f"{label} after retrying with backoff. Wait a minute, then try again."
        )
    if status == 403:
        if reasons & _DISABLED_REASONS:
            api = _api_name(resp)
            return (
                f"Error: the {api} is not enabled in the Google Cloud project of this "
                f"integration's OAuth client, so it cannot {action} {label}. An admin enables "
                f"it in the Google Cloud Console (APIs & Services > Library > {api}); the call "
                "then works as is."
            )
        www_auth = resp.headers.get("www-authenticate", "")
        if reasons & _SCOPE_REASONS or "insufficient_scope" in www_auth:
            return scope_error(need.named, action, label)
        return (
            f"Error: the connected Google account is not allowed to {action} {label}. Ask "
            f"the file's owner to share it with that account as {role}."
        )
    detail = err or f"Error: Google returned HTTP {status}."
    return f"{detail} (file: {label})"


class TokenScopes:
    """The scopes Google granted the integration's current token (``tokeninfo``)."""

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers
        self._token: str | None = None
        self._scopes: frozenset[str] | None = None

    async def missing(self, client: AutoRefreshClient, need: ScopeNeed) -> str | None:
        """*need*'s scope name when the token provably lacks it, else ``None``.

        ``None`` as well when Google does not say (an expired token about to be
        refreshed): the call then runs, and a scope refusal is still read off
        its 403.
        """
        token = self._headers.get("Authorization", "")
        if token != self._token:
            self._token, self._scopes = token, await self._lookup(client)
        if self._scopes is None or self._scopes & need.accepted:
            return None
        return need.named

    async def _lookup(self, client: AutoRefreshClient) -> frozenset[str] | None:
        # The token rides in the Authorization header, never in a URL.
        resp, err = await safe_request(
            client,
            "post",
            _TOKENINFO_URL,
            _PROVIDER,
            "read the token's scopes",
            expected_statuses={400, 401},
            headers={**self._headers, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if err or resp is None:
            return None
        try:
            scope = resp.json().get("scope")
        except (ValueError, AttributeError):
            return None
        return frozenset(scope.split()) if isinstance(scope, str) else None


# ---------------------------------------------------------------------------
# The document's text: what a quote or a find is matched against
# ---------------------------------------------------------------------------


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass
class DocSegment:
    """One searchable stretch of a Doc: a tab's body, header, footer or footnote.

    ``text`` is the concatenated text runs, ``_OBJECT`` standing for anything
    else; ``pieces`` maps it back to the document's UTF-16 indexes as
    ``(text offset, start index, end index, is a text run)``.
    """

    tab_id: str
    tab_title: str
    segment_id: str
    kind: str
    text: str = ""
    pieces: list[tuple[int, int, int, bool]] = field(default_factory=list)

    def start_index(self, offset: int) -> int:
        """The document index of the character at *offset*."""
        piece = self.pieces[bisect.bisect_right([p[0] for p in self.pieces], offset) - 1]
        position, start, _end, is_text = piece
        return start + _u16(self.text[position:offset]) if is_text else start

    def end_index(self, offset: int) -> int:
        """The document index just after the character before *offset*."""
        piece = self.pieces[bisect.bisect_right([p[0] for p in self.pieces], offset - 1) - 1]
        position, start, end, is_text = piece
        return start + _u16(self.text[position:offset]) if is_text else end


def _segment(content: Any, tab_id: str, tab_title: str, segment_id: str, kind: str) -> DocSegment:
    segment = DocSegment(tab_id, tab_title, segment_id, kind)
    parts: list[str] = []
    offset = 0
    last_end: int | None = None

    def add(text: str, start: int, end: int, is_text: bool) -> None:
        nonlocal offset, last_end
        if last_end is not None and start != last_end:
            parts.append(_OBJECT)
            segment.pieces.append((offset, last_end, start, False))
            offset += 1
        parts.append(text)
        segment.pieces.append((offset, start, end, is_text))
        offset += len(text)
        last_end = end

    def walk(elements: Any) -> None:
        for element in elements or []:
            if not isinstance(element, dict):
                continue
            if "paragraph" in element:
                for item in (element["paragraph"] or {}).get("elements") or []:
                    start = int(item.get("startIndex", 0))
                    end = int(item.get("endIndex", start))
                    run = item.get("textRun")
                    if run is not None:
                        if run.get("content"):
                            add(run["content"], start, end, True)
                    elif end > start:
                        add(_OBJECT, start, end, False)
            elif "table" in element:
                for row in (element["table"] or {}).get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        walk(cell.get("content"))
            elif "tableOfContents" in element:
                walk((element["tableOfContents"] or {}).get("content"))

    walk(content)
    segment.text = "".join(parts)
    return segment


def _tabs(document: dict) -> list[dict]:
    """Every tab of *document*, child tabs after their parent."""
    found: list[dict] = []

    def visit(tab: Any) -> None:
        if isinstance(tab, dict):
            found.append(tab)
            for child in tab.get("childTabs") or []:
                visit(child)

    for tab in document.get("tabs") or []:
        visit(tab)
    if not found and "body" in document:
        found.append({"tabProperties": {}, "documentTab": document})
    return found


def doc_segments(document: dict, *, body_only: bool = False) -> list[DocSegment]:
    """The searchable segments of every tab: bodies, then headers, footers, footnotes."""
    segments: list[DocSegment] = []
    for tab in _tabs(document):
        props = tab.get("tabProperties") or {}
        tab_id, title = str(props.get("tabId") or ""), str(props.get("title") or "")
        content = tab.get("documentTab") or {}
        segments.append(_segment((content.get("body") or {}).get("content"), tab_id, title, "", "body"))
        if body_only:
            continue
        for kind, key in (("header", "headers"), ("footer", "footers"), ("footnote", "footnotes")):
            for segment_id, part in (content.get(key) or {}).items():
                segments.append(_segment((part or {}).get("content"), tab_id, title, segment_id, kind))
    return segments


def _pattern(needle: str, match_case: bool) -> re.Pattern[str]:
    return re.compile(re.escape(needle), 0 if match_case else re.IGNORECASE)


def _context(text: str, start: int, end: int, width: int = 60) -> str:
    def clean(part: str) -> str:
        return re.sub(r"\s+", " ", part.replace(_OBJECT, " "))

    before = clean(text[max(0, start - width) : start])
    after = clean(text[end : end + width])
    return f"...{before}>>{clean(text[start:end])}<<{after}..."


def _where(segment: DocSegment, multi_tab: bool) -> str:
    parts = []
    if multi_tab:
        parts.append(f"tab '{segment.tab_title or segment.tab_id}'")
    parts.append(segment.kind)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# The document's text as an agent reads it
# ---------------------------------------------------------------------------

_HEADINGS = {
    "TITLE": "# ",
    "HEADING_1": "# ",
    "HEADING_2": "## ",
    "HEADING_3": "### ",
    "HEADING_4": "#### ",
    "HEADING_5": "##### ",
    "HEADING_6": "###### ",
}
_ORDERED_GLYPHS = frozenset({"DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA", "UPPER_ROMAN", "ROMAN"})


class _Renderer:
    """One tab's body as plain text: headings, lists, tables, chips, footnotes.

    Text runs are kept verbatim inside a paragraph, so a sentence copied from
    it is a quote ``add_google_doc_comment`` and ``edit_google_doc`` find.
    """

    def __init__(self, tab: dict) -> None:
        self._tab = tab
        self._lists = tab.get("lists") or {}
        self._footnotes = tab.get("footnotes") or {}
        self._counters: dict[tuple[str, int], int] = {}
        self._notes: list[tuple[str, str]] = []
        self.suggested = False

    def render(self) -> str:
        lines = self._content((self._tab.get("body") or {}).get("content"))
        for number, footnote_id in self._notes:
            note = " ".join(
                line.strip()
                for line in self._content((self._footnotes.get(footnote_id) or {}).get("content"))
                if line.strip()
            )
            lines.append(f"[^{number}]: {note}")
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    def _content(self, content: Any) -> list[str]:
        lines: list[str] = []
        for element in content or []:
            if not isinstance(element, dict):
                continue
            if "paragraph" in element:
                lines.append(self._paragraph(element["paragraph"] or {}))
            elif "table" in element:
                lines.append("")
                for row in (element["table"] or {}).get("tableRows") or []:
                    cells = [
                        " ".join(line.strip() for line in self._content(cell.get("content")) if line.strip())
                        for cell in row.get("tableCells") or []
                    ]
                    lines.append("| " + " | ".join(cells) + " |")
                lines.append("")
        return lines

    def _paragraph(self, paragraph: dict) -> str:
        text = "".join(self._element(item) for item in paragraph.get("elements") or [])
        text = text.rstrip("\n").replace("\u000b", "\n")
        bullet = paragraph.get("bullet")
        if isinstance(bullet, dict):
            level = int(bullet.get("nestingLevel") or 0)
            return "  " * level + self._marker(str(bullet.get("listId") or ""), level) + text
        style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType") or ""
        return _HEADINGS[style] + text if text and style in _HEADINGS else text

    def _element(self, item: dict) -> str:
        if "textRun" in item:
            run = item["textRun"] or {}
            content = run.get("content") or ""
            newline = "\n" if content.endswith("\n") else ""
            core = content[:-1] if newline else content
            if core and run.get("suggestedDeletionIds"):
                self.suggested, core = True, "{--" + core + "--}"
            elif core and run.get("suggestedInsertionIds"):
                self.suggested, core = True, "{++" + core + "++}"
            return core + newline
        if "person" in item:
            props = (item["person"] or {}).get("personProperties") or {}
            return str(props.get("name") or props.get("email") or "")
        if "richLink" in item:
            props = (item["richLink"] or {}).get("richLinkProperties") or {}
            title, uri = props.get("title") or "", props.get("uri") or ""
            return f"[{title}]({uri})" if title and uri else str(title or uri)
        if "dateElement" in item:
            props = (item["dateElement"] or {}).get("dateElementProperties") or {}
            return str(props.get("displayText") or "")
        if "footnoteReference" in item:
            ref = item["footnoteReference"] or {}
            number = str(ref.get("footnoteNumber") or len(self._notes) + 1)
            self._notes.append((number, str(ref.get("footnoteId") or "")))
            return f"[^{number}]"
        if "inlineObjectElement" in item:
            return "[image]"
        if "horizontalRule" in item:
            return "---"
        if "equation" in item:
            return "[equation]"
        return ""

    def _marker(self, list_id: str, level: int) -> str:
        for key in [key for key in self._counters if key[0] == list_id and key[1] > level]:
            del self._counters[key]
        levels = ((self._lists.get(list_id) or {}).get("listProperties") or {}).get("nestingLevels") or []
        spec = levels[level] if level < len(levels) and isinstance(levels[level], dict) else {}
        if spec.get("glyphType") not in _ORDERED_GLYPHS:
            return "- "
        key = (list_id, level)
        number = self._counters[key] + 1 if key in self._counters else int(spec.get("startNumber") or 1)
        self._counters[key] = number
        return f"{number}. "


def render_document(document: dict) -> tuple[str, bool]:
    """``(text, has pending suggestions)`` for every tab of *document*."""
    tabs = _tabs(document)
    blocks: list[str] = []
    suggested = False
    for tab in tabs:
        renderer = _Renderer(tab.get("documentTab") or {})
        text = renderer.render()
        suggested = suggested or renderer.suggested
        if len(tabs) > 1:
            title = (tab.get("tabProperties") or {}).get("title") or "Untitled tab"
            text = f"=== Tab: {title} ===\n{text}"
        blocks.append(text)
    return "\n\n".join(blocks).strip(), suggested


# ---------------------------------------------------------------------------
# Comment threads
# ---------------------------------------------------------------------------

_AUTHOR_FIELDS = "author(displayName,emailAddress,me)"
_COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,quotedFileContent(value),resolved,deleted,"
    f"createdTime,modifiedTime,{_AUTHOR_FIELDS},"
    f"replies(id,content,action,deleted,createdTime,{_AUTHOR_FIELDS}))"
)


def _when(value: Any) -> str:
    match = _WHEN_RE.match(str(value or ""))
    return f"{match.group(1)} {match.group(2)} UTC" if match else str(value or "")


def _who(author: Any) -> str:
    author = author if isinstance(author, dict) else {}
    name = author.get("displayName") or "Unknown"
    email = author.get("emailAddress")
    who = f"{name} <{email}>" if email else str(name)
    return f"{who} (you)" if author.get("me") else who


def _indented(text: str, prefix: str) -> str:
    return text.replace("\n", "\n" + prefix)


def _byline(post: dict) -> str:
    when = _when(post.get("createdTime"))
    return f"{_who(post.get('author'))}, {when}" if when else _who(post.get("author"))


def _thread_lines(index: int, thread: dict) -> list[str]:
    status = "resolved" if thread.get("resolved") else "open"
    lines = [f"{index}. Comment {thread.get('id', '')} [{status}]", f"   By {_byline(thread)}"]
    quote = (thread.get("quotedFileContent") or {}).get("value")
    if quote:
        lines.append(f'   On: "{_indented(_short(quote, 300), "   ")}"')
    lines.append("   " + _indented(_short(thread.get("content") or "", 2000), "   "))
    replies = [r for r in thread.get("replies") or [] if isinstance(r, dict) and not r.get("deleted")]
    if replies:
        lines.append(f"   Replies ({len(replies)}):")
        for reply in replies:
            action = {"resolve": " [resolved the thread]", "reopen": " [reopened the thread]"}.get(
                reply.get("action") or "", ""
            )
            body = _indented(_short(reply.get("content") or "", 1000), "     ")
            lines.append(f"   - {_byline(reply)}{action}" + (f": {body}" if body else ""))
    return lines


def format_threads(
    threads: list[dict],
    *,
    heading: str,
    include_resolved: bool,
    max_results: int,
    complete: bool,
) -> str:
    """The threads as ``list_google_doc_comments`` returns them."""
    resolved = sum(1 for thread in threads if thread.get("resolved"))
    shown = threads if include_resolved else [t for t in threads if not t.get("resolved")]
    summary = f"{len(threads) - resolved} open, {resolved} resolved"
    if not include_resolved and resolved:
        summary += "; resolved threads are not shown (include_resolved=true lists them)"
    if not shown:
        return f"{heading}: none open ({summary})." if threads else f"{heading}: none."
    lines = [f"{heading} ({summary}):", ""]
    for index, thread in enumerate(shown[:max_results], 1):
        lines.extend(_thread_lines(index, thread))
        lines.append("")
    if len(shown) > max_results:
        lines.append(
            f"... {len(shown) - max_results} more thread(s) not shown "
            "(list_google_doc_comments with a higher max_results lists them)."
        )
    if not complete:
        lines.append(
            f"(Only the first {_COMMENT_PAGE_SIZE * _MAX_COMMENT_PAGES} threads were read.)"
        )
    lines.append(
        "Reply with reply_google_doc_comment(file_id, comment_id, content) or close a "
        "thread with resolve_google_doc_comment(file_id, comment_id)."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Edit:
    find: str
    replace: str
    match_case: bool
    expected_count: int | None


@dataclass
class _Planned:
    edit: _Edit
    count: int
    contexts: list[str]
    problem: str | None


def _parse_edits(raw: Any) -> tuple[list[_Edit], str | None]:
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [], "Error: edits must be a list of {find, replace} objects."
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        return [], (
            "Error: edits is required: a list of {find, replace, match_case, expected_count} objects."
        )
    if len(value) > _MAX_EDITS:
        return [], f"Error: at most {_MAX_EDITS} edits per call ({len(value)} given)."
    edits: list[_Edit] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            return [], f"Error: edit {index} is not an object with find and replace."
        find = item.get("find")
        replace = item.get("replace", "")
        if not isinstance(find, str) or not find:
            return [], f"Error: edit {index} has no find text."
        if replace is None:
            replace = ""
        if not isinstance(replace, str):
            return [], f"Error: edit {index}'s replace must be text."
        if find == replace:
            return [], f"Error: edit {index} replaces its find text with itself."
        expected = item.get("expected_count")
        if expected in (None, ""):
            expected_count = None
        else:
            try:
                expected_count = int(expected)
            except (TypeError, ValueError):
                expected_count = 0
            if expected_count < 1:
                return [], f"Error: edit {index}'s expected_count must be a whole number of at least 1."
        edits.append(_Edit(find, replace, bool_arg(item.get("match_case"), True), expected_count))
    return edits, None


def _plan(segments: list[DocSegment], edits: list[_Edit]) -> list[_Planned]:
    """Each edit counted on the text the edits before it leave, as Google applies them."""
    multi_tab = len({segment.tab_id for segment in segments}) > 1
    texts = [segment.text for segment in segments]
    plans: list[_Planned] = []
    for edit in edits:
        pattern = _pattern(edit.find, edit.match_case)
        count = 0
        contexts: list[str] = []
        for segment, text in zip(segments, texts, strict=True):
            for match in pattern.finditer(text):
                count += 1
                if len(contexts) < _MAX_CONTEXTS:
                    contexts.append(
                        f"{_where(segment, multi_tab)}: {_context(text, match.start(), match.end())}"
                    )
        problem = None
        if count == 0:
            hints = near_matches(texts, edit.find)
            problem = "no match in the Doc"
            if hints:
                problem += "; its exact text is " + " or ".join(f'"{_short(h, 200)}"' for h in hints)
        elif edit.expected_count is not None and count > edit.expected_count:
            problem = f"{count} matches, more than expected_count={edit.expected_count}"
        plans.append(_Planned(edit, count, contexts, problem))
        if count:
            texts = [pattern.sub(lambda _match, text=edit.replace: text, t) for t in texts]
    return plans


def _plan_lines(plans: list[_Planned], changed: list[int] | None = None) -> list[str]:
    lines: list[str] = []
    for index, plan in enumerate(plans, 1):
        edit = plan.edit
        head = f'{index}. "{_short(edit.find, 120)}" -> "{_short(edit.replace, 120)}"'
        if not edit.match_case:
            head += " (any case)"
        lines.append(head)
        if plan.problem:
            lines.append(f"   REFUSED: {plan.problem}")
            continue
        status = f"   {plan.count} match(es)"
        if changed is not None and index - 1 < len(changed) and changed[index - 1] != plan.count:
            status += f", {changed[index - 1]} changed by Google"
        lines.append(status)
        lines.extend(f"   - {context}" for context in plan.contexts)
        if plan.count > len(plan.contexts):
            lines.append(f"   - ... and {plan.count - len(plan.contexts)} more")
    return lines


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

_BAD_FILE_ID = (
    "Error: file_id is required: the Google Doc's Drive file ID or its URL "
    "(from search_google_drive or list_google_drive)."
)


class GoogleDocTools:
    """The Google Docs review tools of one ``build_tools`` call."""

    def __init__(self, headers: dict[str, str], client_factory: Callable[[], AutoRefreshClient]) -> None:
        self._client = client_factory
        self._scopes = TokenScopes(headers)
        # Set once documents.get says the Docs API is off in the OAuth client's
        # project, so later reads go straight to Drive's export.
        self._docs_api_off: str | None = None

    # -- shared calls ---------------------------------------------------------

    async def _file(self, client: AutoRefreshClient, file_id: str, action: str) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{_DRIVE_API}/files/{_path(file_id)}",
            _PROVIDER,
            "read file metadata",
            expected_statuses=set(),
            params={
                "fields": "id,name,mimeType,webViewLink,capabilities(canComment,canEdit)",
                "supportsAllDrives": "true",
            },
        )
        if err:
            return None, failure(
                resp, err, action=action, label=f"file {file_id}", need=_READ_DRIVE, role="a commenter"
            )
        return resp.json(), None

    async def _document(
        self, client: AutoRefreshClient, file_id: str, *, inline: bool
    ) -> tuple[dict | None, httpx.Response | None, str | None]:
        params = {"includeTabsContent": "true"}
        if inline:
            # The only view whose indexes a batchUpdate can use.
            params["suggestionsViewMode"] = "SUGGESTIONS_INLINE"
        resp, err = await safe_request(
            client,
            "get",
            f"{_DOCS_API}/documents/{_path(file_id)}",
            _PROVIDER,
            "read the document",
            expected_statuses=set(),
            params=params,
        )
        if err:
            return None, resp, err
        return resp.json(), None, None

    async def _threads(
        self, client: AutoRefreshClient, file_id: str
    ) -> tuple[list[dict] | None, httpx.Response | None, str | None, bool]:
        threads: list[dict] = []
        page_token = ""
        for _page in range(_MAX_COMMENT_PAGES):
            params = {"fields": _COMMENT_FIELDS, "pageSize": str(_COMMENT_PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_API}/files/{_path(file_id)}/comments",
                _PROVIDER,
                "list comments",
                expected_statuses=set(),
                params=params,
            )
            if err:
                return None, resp, err, False
            data = resp.json()
            threads.extend(
                thread
                for thread in data.get("comments") or []
                if isinstance(thread, dict) and not thread.get("deleted")
            )
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                return threads, None, None, True
        return threads, None, None, False

    async def _batch_update(
        self, client: AutoRefreshClient, file_id: str, body: dict
    ) -> tuple[httpx.Response | None, str | None]:
        return await safe_request(
            client,
            "post",
            f"{_DOCS_API}/documents/{_path(file_id)}:batchUpdate",
            _PROVIDER,
            "update the document",
            expected_statuses=set(),
            json=body,
        )

    # -- read_google_doc ------------------------------------------------------

    async def document_text(
        self, client: AutoRefreshClient, file_id: str, rest_hint: str
    ) -> tuple[str | None, str | None]:
        """``(text, None)`` for a Doc read through the Docs API, without comment
        markers; ``(None, reason)`` when the caller falls back on Drive's export."""
        if self._docs_api_off:
            return None, self._docs_api_off
        document, resp, err = await self._document(client, file_id, inline=False)
        if document is None:
            reason = failure(resp, err, action="read", label="the Doc", need=_READ_DOC, role="a viewer")
            if resp is not None and _error_reasons(resp) & _DISABLED_REASONS:
                self._docs_api_off = reason
            return None, reason
        text, suggested = render_document(document)
        note = (
            "(Pending suggestions are shown inline as {++inserted++} and {--deleted--}.)\n\n"
            if suggested
            else ""
        )
        return note + cut_inline(text, MAX_DOCUMENT_CHARS, rest_hint), None

    async def comments_section(self, client: AutoRefreshClient, file_id: str, label: str) -> str:
        """The open threads of a file, for ``read_google_doc``; a note when unreadable."""
        threads, resp, err, complete = await self._threads(client, file_id)
        if threads is None:
            reason = failure(
                resp, err, action="list the comments of", label=label, need=_READ_DRIVE, role="a commenter"
            )
            return "Comments: not available. " + _as_note(reason)
        return format_threads(
            threads,
            heading="Comments",
            include_resolved=False,
            max_results=_READ_MAX_THREADS,
            complete=complete,
        )

    # -- list_google_doc_comments ---------------------------------------------

    async def list_google_doc_comments(
        self, file_id: str, include_resolved: bool = False, max_results: int = 100
    ) -> str:
        fid = parse_file_id(file_id)
        if fid is None:
            return _BAD_FILE_ID
        action = "list the comments of"
        async with self._client() as client:
            meta, err = await self._file(client, fid, action)
            if err:
                return err
            label = _label(meta, fid)
            missing = await self._scopes.missing(client, _READ_DRIVE)
            if missing:
                return scope_error(missing, action, label)
            threads, resp, err, complete = await self._threads(client, fid)
            if threads is None:
                return failure(resp, err, action=action, label=label, need=_READ_DRIVE, role="a commenter")
        return (
            format_threads(
                threads,
                heading=f"Comment threads on {label}",
                include_resolved=bool_arg(include_resolved),
                max_results=_bounded(max_results, 100, 1, 500),
                complete=complete,
            )
            + _link(meta)
        )

    # -- add_google_doc_comment -----------------------------------------------

    async def add_google_doc_comment(self, file_id: str, content: str, quoted_text: str = "") -> str:
        fid = parse_file_id(file_id)
        if fid is None:
            return _BAD_FILE_ID
        text = str(content or "").strip()
        if not text:
            return "Error: content is required: the text of the comment."
        quote = str(quoted_text or "").strip()
        action = "comment on"
        async with self._client() as client:
            meta, err = await self._file(client, fid, action)
            if err:
                return err
            label = _label(meta, fid)
            if (meta.get("capabilities") or {}).get("canComment") is False:
                return (
                    f"Error: the connected Google account cannot comment on {label}. Ask the "
                    "file's owner to share it with that account as a commenter."
                )
            if not quote:
                return await self._file_comment(client, fid, meta, label, text)
            if meta.get("mimeType") != GOOGLE_DOC_MIME:
                return (
                    f"Error: quoted_text attaches a comment to a passage of a Google Doc, and "
                    f"{label} is a {meta.get('mimeType') or 'non-Doc'} file. Post the comment "
                    "without quoted_text, and quote the passage in its content."
                )
            size = len(text.encode("utf-8"))
            if size > _ANCHORED_COMMENT_MAX_BYTES:
                return (
                    f"Error: Google takes at most {_ANCHORED_COMMENT_MAX_BYTES} bytes of text in a "
                    f"comment attached to a passage; this one has {size}. Shorten it or split it."
                )
            missing = await self._scopes.missing(client, _WRITE_DOC)
            if missing:
                return scope_error(missing, action, label)
            document, resp, err = await self._document(client, fid, inline=True)
            if document is None:
                return failure(resp, err, action=action, label=label, need=_WRITE_DOC, role="a commenter")
            segments = doc_segments(document, body_only=True)
            matches = [
                (segment, match)
                for segment in segments
                for match in _pattern(quote, True).finditer(segment.text)
            ]
            if len(matches) != 1:
                return _quote_refusal(quote, label, segments, matches)
            segment, match = matches[0]
            anchor: dict[str, Any] = {
                "startIndex": segment.start_index(match.start()),
                "endIndex": segment.end_index(match.end()),
            }
            if segment.tab_id:
                anchor["tabId"] = segment.tab_id
            resp, err = await self._batch_update(
                client, fid, {"requests": [{"insertComment": {"content": text, "range": anchor}}]}
            )
            if err:
                return failure(resp, err, action=action, label=label, need=_WRITE_DOC, role="a commenter")
        data = resp.json()
        if data.get("commentUpdateState") == "ALL_FAILED_UNKNOWN_REASON":
            return f"Error: Google did not save the comment on {label}; nothing was posted. Try again."
        replies = data.get("replies") or [{}]
        thread = ((replies[0] or {}).get("insertComment") or {}).get("commentThread") or {}
        where = f" (tab '{segment.tab_title}')" if len({s.tab_id for s in segments}) > 1 else ""
        return (
            f'Commented on {label}, attached to "{_short(quote, 200)}"{where}. '
            f"Comment ID: {thread.get('commentId', '')}. The Doc shows it on that passage, "
            "under the connected account's name." + _link(meta)
        )

    async def _file_comment(
        self, client: AutoRefreshClient, fid: str, meta: dict, label: str, text: str
    ) -> str:
        action = "comment on"
        missing = await self._scopes.missing(client, _COMMENT)
        if missing:
            return scope_error(missing, action, label)
        resp, err = await safe_request(
            client,
            "post",
            f"{_DRIVE_API}/files/{_path(fid)}/comments",
            _PROVIDER,
            "create a comment",
            expected_statuses=set(),
            params={"fields": "id,createdTime"},
            json={"content": text},
        )
        if err:
            return failure(resp, err, action=action, label=label, need=_COMMENT, role="a commenter")
        return (
            f"Commented on {label}, on the file as a whole (no passage given). "
            f"Comment ID: {resp.json().get('id', '')}." + _link(meta)
        )

    # -- reply_google_doc_comment / resolve_google_doc_comment ---------------

    async def reply_google_doc_comment(
        self, file_id: str, comment_id: str, content: str = "", action: str = ""
    ) -> str:
        fid = parse_file_id(file_id)
        if fid is None:
            return _BAD_FILE_ID
        cid = str(comment_id or "").strip()
        if not _ID_RE.fullmatch(cid):
            return "Error: comment_id is required: a comment's ID from list_google_doc_comments."
        verb = str(action or "").strip().lower()
        if verb not in ("", "resolve", "reopen"):
            return "Error: action must be 'resolve', 'reopen', or empty for a plain reply."
        text = str(content or "").strip()
        if not text and not verb:
            return "Error: content is required for a reply that neither resolves nor reopens the thread."
        doing = "reply to a comment on"
        async with self._client() as client:
            meta, err = await self._file(client, fid, doing)
            if err:
                return err
            label = _label(meta, fid)
            if (meta.get("capabilities") or {}).get("canComment") is False:
                return (
                    f"Error: the connected Google account cannot comment on {label}. Ask the "
                    "file's owner to share it with that account as a commenter."
                )
            missing = await self._scopes.missing(client, _COMMENT)
            if missing:
                return scope_error(missing, doing, label)
            body: dict[str, str] = {}
            if text:
                body["content"] = text
            if verb:
                body["action"] = verb
            resp, err = await safe_request(
                client,
                "post",
                f"{_DRIVE_API}/files/{_path(fid)}/comments/{_path(cid)}/replies",
                _PROVIDER,
                "reply to a comment",
                expected_statuses=set(),
                params={"fields": "id,action,createdTime"},
                json=body,
            )
            if err:
                return failure(
                    resp,
                    err,
                    action=doing,
                    label=label,
                    need=_COMMENT,
                    role="a commenter",
                    not_found=(
                        f"Error: {label} has no comment {cid} (or it was deleted). List its "
                        "threads with list_google_doc_comments."
                    ),
                )
        reply_id = resp.json().get("id", "")
        if not text:
            done = "Resolved" if verb == "resolve" else "Reopened"
            return f"{done} comment {cid} on {label}." + _link(meta)
        then = {"resolve": " and resolved the thread", "reopen": " and reopened the thread"}.get(verb, "")
        return f"Replied to comment {cid} on {label}{then}. Reply ID: {reply_id}." + _link(meta)

    async def resolve_google_doc_comment(self, file_id: str, comment_id: str, reply: str = "") -> str:
        return await self.reply_google_doc_comment(file_id, comment_id, content=reply, action="resolve")

    # -- edit_google_doc ------------------------------------------------------

    async def edit_google_doc(
        self, file_id: str, edits: Any = None, dry_run: bool = False, mode: str = "suggest"
    ) -> str:
        fid = parse_file_id(file_id)
        if fid is None:
            return _BAD_FILE_ID
        parsed, err = _parse_edits(edits)
        if err:
            return err
        how = str(mode or "suggest").strip().lower()
        if how not in ("suggest", "direct"):
            return "Error: mode must be 'suggest' (the default: tracked suggestions) or 'direct'."
        rehearse = bool_arg(dry_run)
        suggest = how == "suggest"
        action = "read" if rehearse else "suggest edits to" if suggest else "edit"
        async with self._client() as client:
            meta, err = await self._file(client, fid, action)
            if err:
                return err
            label = _label(meta, fid)
            if meta.get("mimeType") != GOOGLE_DOC_MIME:
                return (
                    f"Error: edit_google_doc changes the text of a Google Doc, and {label} is a "
                    f"{meta.get('mimeType') or 'non-Doc'} file."
                )
            capabilities = meta.get("capabilities") or {}
            if not rehearse and not suggest and capabilities.get("canEdit") is False:
                return (
                    f"Error: the connected Google account cannot edit {label}. Ask the file's "
                    "owner to share it with that account as an editor, or propose the change "
                    "with mode='suggest' (needs comment access)."
                )
            if not rehearse and suggest and capabilities.get("canComment") is False:
                return (
                    f"Error: the connected Google account cannot suggest edits to {label}. Ask "
                    "the file's owner to share it with that account as a commenter."
                )
            missing = await self._scopes.missing(client, _READ_DOC if rehearse else _WRITE_DOC)
            if missing:
                return scope_error(missing, action, label)
            document, resp, err = await self._document(client, fid, inline=True)
            if document is None:
                return failure(resp, err, action=action, label=label, need=_READ_DOC, role="a commenter")
            plans = _plan(doc_segments(document), parsed)
            if any(plan.problem for plan in plans):
                return "\n".join(
                    [
                        f"Error: refused to edit {label}; nothing was changed. Every find must "
                        "match, and no more often than its expected_count:",
                        *_plan_lines(plans),
                    ]
                )
            applied_as = "as suggestions" if suggest else "directly"
            if rehearse:
                return "\n".join(
                    [
                        f"Dry run on {label}: nothing was changed. Each find and its matches:",
                        *_plan_lines(plans),
                        f"Run edit_google_doc again with dry_run=false to apply them {applied_as}.",
                    ]
                )
            write_control: dict[str, str] = {"writeMode": "SUGGEST" if suggest else "EDIT"}
            if document.get("revisionId"):
                # Applied only to the text that was counted: an edit made since fails.
                write_control["requiredRevisionId"] = str(document["revisionId"])
            requests = [
                {
                    "replaceAllText": {
                        "containsText": {"text": plan.edit.find, "matchCase": plan.edit.match_case},
                        "replaceText": plan.edit.replace,
                    }
                }
                for plan in plans
            ]
            resp, err = await self._batch_update(
                client, fid, {"requests": requests, "writeControl": write_control}
            )
            if err:
                if resp is not None and resp.status_code == 400 and "revision" in _error_message(resp).lower():
                    return (
                        f"Error: {label} changed since it was read, so nothing was applied. Run "
                        "edit_google_doc again to count the matches on its current text."
                    )
                return failure(
                    resp, err, action=action, label=label, need=_WRITE_DOC,
                    role="an editor" if not suggest else "a commenter",
                )
        data = resp.json()
        replies = data.get("replies") or []
        changed = [
            int(((reply or {}).get("replaceAllText") or {}).get("occurrencesChanged") or 0)
            for reply in replies
        ]
        reported = len(changed) == len(plans)
        total = sum(changed) if reported else sum(plan.count for plan in plans)
        if suggest:
            head = (
                f"Suggested {total} replacement(s) in {label}: the Doc shows them as "
                "suggestions its editors accept or reject."
            )
        else:
            head = f"Replaced {total} occurrence(s) in {label} directly."
        lines = [head, *_plan_lines(plans, changed if reported else None)]
        if suggest and data.get("commentUpdateState") == "ALL_FAILED_UNKNOWN_REASON":
            lines.append(
                "Warning: Google reported that the suggestions failed to save. Open the Doc and "
                "check whether the text changed directly."
            )
        return "\n".join(lines) + _link(meta)

    # -- registration ---------------------------------------------------------

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {
            "list_google_doc_comments": self.list_google_doc_comments,
            "add_google_doc_comment": self.add_google_doc_comment,
            "reply_google_doc_comment": self.reply_google_doc_comment,
            "resolve_google_doc_comment": self.resolve_google_doc_comment,
            "edit_google_doc": self.edit_google_doc,
        }

    @staticmethod
    def definitions() -> list[ToolDefinition]:
        file_id = {
            "type": "string",
            "description": "The Google Doc's Drive file ID, or its URL",
        }
        comment_id = {
            "type": "string",
            "description": "The comment thread's ID (from list_google_doc_comments or add_google_doc_comment)",
        }
        return [
            ToolDefinition(
                name="list_google_doc_comments",
                description=(
                    "List the comment threads of a Google Doc (or any Drive file), as its "
                    "collaborators see them: each thread's comment ID, author, the text it is "
                    "attached to, its content, open or resolved status, and every reply. Open "
                    "threads only unless include_resolved. Use the IDs with "
                    "reply_google_doc_comment and resolve_google_doc_comment. read_google_doc "
                    "lists the open threads too, after the text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": file_id,
                        "include_resolved": {
                            "type": "boolean",
                            "description": "Also list resolved threads (default false)",
                            "default": False,
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max threads listed (default 100, at most 500)",
                            "default": 100,
                        },
                    },
                    "required": ["file_id"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="add_google_doc_comment",
                description=(
                    "Post a comment on a Google Doc under the connected user's name, exactly as "
                    "written (nothing is prefixed). Give quoted_text to attach it to that "
                    "passage: the Doc highlights it like a comment made in the editor. "
                    "quoted_text must be copied verbatim from read_google_doc and occur exactly "
                    "once in the Doc's body; otherwise nothing is posted and the error shows the "
                    "matches or the Doc's exact text to quote instead (quote a longer passage to "
                    "make it unique). Without quoted_text the comment is on the whole file (any "
                    "Drive file). Post one comment per finding. To propose new wording, prefer "
                    "edit_google_doc, which writes it as a suggestion."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": file_id,
                        "content": {
                            "type": "string",
                            "description": "The comment's text (at most 2048 bytes when attached to a passage)",
                        },
                        "quoted_text": {
                            "type": "string",
                            "description": "The exact passage of the Doc the comment is about; it must occur once",
                            "default": "",
                        },
                    },
                    "required": ["file_id", "content"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="reply_google_doc_comment",
                description=(
                    "Reply to a comment thread of a Google Doc (or any Drive file) under the "
                    "connected user's name. action 'resolve' also resolves the thread, 'reopen' "
                    "reopens a resolved one; content may then be empty. Find comment IDs with "
                    "list_google_doc_comments."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": file_id,
                        "comment_id": comment_id,
                        "content": {
                            "type": "string",
                            "description": "The reply's text (required unless action is set)",
                            "default": "",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["", "resolve", "reopen"],
                            "description": "Optional: 'resolve' or 'reopen' the thread with this reply",
                            "default": "",
                        },
                    },
                    "required": ["file_id", "comment_id"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="resolve_google_doc_comment",
                description=(
                    "Resolve a comment thread of a Google Doc (or any Drive file), with an "
                    "optional closing reply, under the connected user's name. Reopen one with "
                    "reply_google_doc_comment(action='reopen')."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": file_id,
                        "comment_id": comment_id,
                        "reply": {
                            "type": "string",
                            "description": "Optional message posted with the resolution",
                            "default": "",
                        },
                    },
                    "required": ["file_id", "comment_id"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="edit_google_doc",
                description=(
                    "Change the text of a Google Doc by find-and-replace. mode 'suggest' (the "
                    "default) writes every replacement as a suggestion the Doc's editors accept "
                    "or reject, like Suggesting mode in the editor; use mode 'direct' only when "
                    "the user explicitly asks to edit the text directly. Every occurrence of "
                    "each find in the Doc (all tabs) is replaced. Before "
                    "anything is applied each find is counted on the current text (edits apply "
                    "in order, each on the text the previous ones leave), and the whole call is "
                    "refused, changing nothing, if a find matches nothing or more often than its "
                    "expected_count. Run with dry_run=true first to see each match in context. "
                    "Copy find verbatim from read_google_doc."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "file_id": file_id,
                        "edits": {
                            "type": "array",
                            "description": "The replacements, applied in order",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "find": {"type": "string", "description": "Exact text to find"},
                                    "replace": {
                                        "type": "string",
                                        "description": "Text that replaces it (empty deletes it)",
                                    },
                                    "match_case": {
                                        "type": "boolean",
                                        "description": "Match case exactly (default true)",
                                        "default": True,
                                    },
                                    "expected_count": {
                                        "type": "integer",
                                        "description": "Refuse when find matches more often than this",
                                        "minimum": 1,
                                    },
                                },
                                "required": ["find", "replace"],
                            },
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Only show the matches in context, change nothing (default false)",
                            "default": False,
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["suggest", "direct"],
                            "description": "'suggest' (default) for tracked suggestions, 'direct' to change the text",
                            "default": "suggest",
                        },
                    },
                    "required": ["file_id", "edits"],
                },
                source=_SOURCE,
                priority=1,
            ),
        ]


def _quote_refusal(
    quote: str, label: str, segments: list[DocSegment], matches: list[tuple[DocSegment, re.Match[str]]]
) -> str:
    """Why a comment was not posted: its quote occurs zero or several times."""
    if not matches:
        hints = near_matches([segment.text for segment in segments], quote)
        if hints:
            options = " or ".join(f'"{_short(hint, 300)}"' for hint in hints)
            return (
                f"Error: quoted_text does not occur in {label} as written, so nothing was "
                f"posted. The Doc's exact text is {options}: quote it verbatim."
            )
        return (
            f"Error: quoted_text does not occur in the body of {label}, so nothing was posted. "
            "Copy the passage verbatim from read_google_doc."
        )
    multi_tab = len({segment.tab_id for segment in segments}) > 1
    lines = [
        f"Error: quoted_text occurs {len(matches)} times in {label}, so nothing was posted. "
        "Quote a longer passage that occurs once:"
    ]
    lines.extend(
        f"- {_where(segment, multi_tab)}: {_context(segment.text, match.start(), match.end())}"
        for segment, match in matches[:_MAX_CONTEXTS]
    )
    return "\n".join(lines)
