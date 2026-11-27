"""Mail folders of an Outlook mailbox: the tree, and a folder named by its name, path or id.

Graph addresses a mail folder in a URL segment by a well-known name
(``inbox``, ``sentitems``...) or by its id. A custom folder's display name
there is read as a malformed id (``Id is malformed``), so a folder created by
the user could not be read at all. :class:`FolderDirectory` walks
``/me/mailFolders`` and its ``childFolders`` once per tool session and
resolves what an agent writes to the segment Graph takes:

- a well-known name (``inbox``, ``sent``, ``archive``...): unchanged, no read;
- a folder id: passed through, URL-encoded;
- a display name (``Contracts copies``) or a path (``Inbox/Contracts copies``):
  matched case-insensitively against the tree; an ambiguous name is refused
  with its candidate paths, an unknown one with the top-level folders.

The first segment of a path may be a well-known name too, so ``inbox/X``
reads the same folder whatever the mailbox language calls its Inbox.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

#: Common spellings of the Graph well-known folder names.
FOLDER_ALIASES: dict[str, str] = {
    "inbox": "inbox",
    "sent": "sentitems",
    "sentitems": "sentitems",
    "sent items": "sentitems",
    "drafts": "drafts",
    "deleted": "deleteditems",
    "deleteditems": "deleteditems",
    "deleted items": "deleteditems",
    "junk": "junkemail",
    "junkemail": "junkemail",
    "archive": "archive",
}

#: Folders read at most per walk; a mailbox past it is listed in part, and says so.
MAX_FOLDERS = 1000
#: Depth of the walk under the top level.
MAX_DEPTH = 12

_SELECT = "id,displayName,childFolderCount,totalItemCount,unreadItemCount"
# A Graph folder id: one long base64 token (``AAMkAG...``, ``AQMk...``), no whitespace.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]{60,}$")

#: ``fetch(url, params)`` answers the decoded JSON body, or the error text.
Fetch = Callable[[str, dict[str, str] | None], Awaitable[tuple[dict[str, Any] | None, str | None]]]


@dataclass(frozen=True)
class MailFolder:
    id: str
    name: str
    path: str
    parent_path: str
    child_count: int
    total: int
    unread: int


@dataclass(frozen=True)
class ResolvedFolder:
    """The URL segment Graph takes for a folder, and the label an answer names it by."""

    segment: str
    label: str


def looks_like_folder_id(value: str) -> bool:
    return bool(_ID_PATTERN.match(value or ""))


class FolderDirectory:
    """The folder tree of one mailbox, read once and kept for the tool session."""

    def __init__(self, fetch: Fetch, base_url: str) -> None:
        self._fetch = fetch
        self._base_url = base_url
        self._folders: list[MailFolder] | None = None
        self._truncated = False
        self._well_known_ids: dict[str, str] = {}
        self._resolved: dict[str, ResolvedFolder] = {}

    @property
    def truncated(self) -> bool:
        return self._truncated

    async def _pages(self, url: str) -> tuple[list[dict[str, Any]], str | None]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        params: dict[str, str] | None = {"$top": "100", "$select": _SELECT}
        while next_url:
            data, err = await self._fetch(next_url, params)
            if err:
                return items, err
            data = data or {}
            items.extend(v for v in data.get("value") or [] if isinstance(v, dict))
            next_url = data.get("@odata.nextLink") or None
            params = None  # the next link carries its own query
            if len(items) >= MAX_FOLDERS:
                break
        return items, None

    async def folders(self) -> tuple[list[MailFolder], str | None]:
        """Every folder of the mailbox, parents before their children."""
        if self._folders is not None:
            return self._folders, None
        found: list[MailFolder] = []
        queue: list[tuple[str, str, int]] = [(f"{self._base_url}/me/mailFolders", "", 0)]
        while queue:
            url, parent_path, depth = queue.pop(0)
            items, err = await self._pages(url)
            if err:
                return found, err
            for item in items:
                if len(found) >= MAX_FOLDERS:
                    self._truncated = True
                    break
                name = str(item.get("displayName") or "")
                path = f"{parent_path}/{name}" if parent_path else name
                folder = MailFolder(
                    id=str(item.get("id") or ""),
                    name=name,
                    path=path,
                    parent_path=parent_path,
                    child_count=int(item.get("childFolderCount") or 0),
                    total=int(item.get("totalItemCount") or 0),
                    unread=int(item.get("unreadItemCount") or 0),
                )
                found.append(folder)
                if folder.child_count and folder.id:
                    if depth + 1 > MAX_DEPTH:
                        self._truncated = True
                        continue
                    queue.append(
                        (f"{self._base_url}/me/mailFolders/{quote(folder.id, safe='')}/childFolders", path, depth + 1)
                    )
        self._folders = found
        return found, None

    async def _well_known_id(self, name: str) -> tuple[str | None, str | None]:
        if name in self._well_known_ids:
            return self._well_known_ids[name], None
        data, err = await self._fetch(f"{self._base_url}/me/mailFolders/{name}", {"$select": "id"})
        if err:
            return None, err
        folder_id = str((data or {}).get("id") or "")
        self._well_known_ids[name] = folder_id
        return folder_id, None

    @staticmethod
    def _top_level_names(folders: list[MailFolder]) -> str:
        names = [f.name for f in folders if not f.parent_path]
        return ", ".join(f"'{n}'" for n in names) or "none"

    async def resolve(self, spec: str) -> tuple[ResolvedFolder | None, str | None]:
        """The folder *spec* names, or the error that says why it names none."""
        text = (spec or "").strip() or "inbox"
        alias = FOLDER_ALIASES.get(text.lower())
        if alias:
            return ResolvedFolder(alias, alias), None
        if text in self._resolved:
            return self._resolved[text], None
        if looks_like_folder_id(text):
            resolved = ResolvedFolder(quote(text, safe=""), text)
            self._resolved[text] = resolved
            return resolved, None
        folders, err = await self.folders()
        if err:
            return None, f"Error: the mail folders could not be read to find '{text}': {err}"
        segments = [s.strip() for s in text.strip("/").split("/") if s.strip()]
        if len(segments) > 1:
            match, error = await self._by_path(segments, folders, text)
        else:
            match, error = self._by_name(segments[0] if segments else text, folders, text)
        if error or match is None:
            return None, error or self._unknown(text, folders)
        resolved = ResolvedFolder(quote(match.id, safe=""), match.path)
        self._resolved[text] = resolved
        return resolved, None

    async def find(self, spec: str) -> tuple[MailFolder | None, str | None]:
        """The folder of the tree *spec* names (a well-known name, an id, a name or a path)."""
        resolved, err = await self.resolve(spec)
        if err or resolved is None:
            return None, err
        folders, err = await self.folders()
        if err:
            return None, err
        text = (spec or "").strip() or "inbox"
        alias = FOLDER_ALIASES.get(text.lower())
        if alias:
            folder_id, err = await self._well_known_id(alias)
            if err:
                return None, err
        else:
            folder_id = unquote(resolved.segment)
        match = next((f for f in folders if f.id == folder_id), None)
        if match is None:
            return None, self._unknown(text, folders)
        return match, None

    def _by_name(self, name: str, folders: list[MailFolder], text: str) -> tuple[MailFolder | None, str | None]:
        matches = [f for f in folders if f.name.lower() == name.lower()]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            paths = "; ".join(f"'{f.path}'" for f in matches)
            return None, (
                f"Error: more than one mail folder is named '{text}': {paths}. "
                "Pass the path of the one to read (one of the paths above) or its id from list_microsoft_mail_folders."
            )
        return None, self._unknown(text, folders)

    async def _by_path(
        self, segments: list[str], folders: list[MailFolder], text: str
    ) -> tuple[MailFolder | None, str | None]:
        first = segments[0]
        level = [f for f in folders if not f.parent_path and f.name.lower() == first.lower()]
        if not level and first.lower() in FOLDER_ALIASES:
            folder_id, err = await self._well_known_id(FOLDER_ALIASES[first.lower()])
            if err:
                return None, f"Error: the mail folder '{first}' could not be read to find '{text}': {err}"
            level = [f for f in folders if not f.parent_path and f.id == folder_id]
        for segment in segments[1:]:
            parents = {f.path for f in level}
            level = [f for f in folders if f.parent_path in parents and f.name.lower() == segment.lower()]
        if len(level) == 1:
            return level[0], None
        if level:
            paths = "; ".join(f"'{f.path}'" for f in level)
            return None, (
                f"Error: more than one mail folder matches the path '{text}': {paths}. "
                "Pass its id from list_microsoft_mail_folders."
            )
        return None, self._unknown(text, folders)

    def _unknown(self, text: str, folders: list[MailFolder]) -> str:
        cut = " (the mailbox has more folders than were read)" if self._truncated else ""
        return (
            f"Error: no mail folder named '{text}'{cut}. Top-level folders: {self._top_level_names(folders)}. "
            "list_microsoft_mail_folders lists every folder with its path and id."
        )


def render_folders(folders: list[MailFolder], *, truncated: bool, under: str = "") -> str:
    """One line per folder: path | id | child folders | items | unread."""
    if not folders:
        return f"No mail folders under '{under}'." if under else "No mail folders."
    where = f" under '{under}'" if under else ""
    header = f"{len(folders)} mail folders{where}: (path | id | child folders | items | unread)"
    lines = [header]
    lines.extend(f"{f.path} | {f.id} | {f.child_count} | {f.total} | {f.unread}" for f in folders)
    lines.append(
        "Read one with list_microsoft_emails(folder=PATH_OR_ID), or search it with search_microsoft_emails(folder=...)."
    )
    if truncated:
        lines.append(f"Listed in part: the walk stops at {MAX_FOLDERS} folders or {MAX_DEPTH} levels.")
    return "\n".join(lines)
