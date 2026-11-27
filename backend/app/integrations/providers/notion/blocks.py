"""Notion block tools: get, list children, append, update, delete."""

from __future__ import annotations

import json
import re

from app.tools.base import ToolDefinition, ToolResult

from ._common import (
    _BASE_URL,
    _BLOCK_WRITE_RETRY_STATUSES,
    _LIST_CHILDREN_MAX_BLOCKS,
    _LIST_CHILDREN_MAX_DEPTH,
    _LIST_CHILDREN_MAX_FETCHES,
    _NOTION_BLOCK_TYPES,
    _PROVIDER,
    _TEXT_BLOCK_PREFIXES,
    NotionContext,
    _alignment_argument_refusal,
    _alignment_refusal,
    _block_to_text,
    _markdown_link_target,
    _parse_json_arg,
    _rich_text_link,
    _rich_text_to_markdown,
)
from .formatting import (
    CALLOUT_ICON_REMOVAL_REFUSAL,
    MARKDOWN_LAYOUT_SYNTAX,
    MarkdownBlockError,
    _find_block_alignment,
    _parse_inline_markdown,
    _rich_text,
    _sanitize_blocks,
    _text_to_blocks,
    _update_block_fields,
    find_null_callout_icon,
)

# How a write tool refuses a payload that carries ``alignment``: the
# path names the exact key to drop so the agent resends the rest instead of
# retrying the whole payload against a raw Notion 400.
_ALIGNMENT_RETRY = "Remove `alignment` from the payload and resend the rest."
# The three block write tools also declare an ``alignment`` ARGUMENT (absent
# from their schemas) only to refuse it: the executor silently drops kwargs a
# handler does not declare, so ``append_notion_blocks_json(..., alignment=
# "left")`` would otherwise append the blocks and let the agent report an
# alignment it never applied - the same silent drop ``upload_notion_file`` already
# refuses. Checked FIRST, before the JSON is even parsed, so
# a refused alignment costs nothing and the call is resent as-is minus the
# argument.
_NOTHING_APPENDED = "nothing has been appended"
_NOTHING_UPDATED = "nothing has been updated"

# The blocks whose text ``update_notion_block(content=...)`` rewrites from
# Markdown. A code block's text is raw, so it stays a block_json update.
_MARKDOWN_TEXT_BLOCKS = frozenset(_TEXT_BLOCK_PREFIXES) | {"to_do"}
_TO_DO_PREFIXES = ("☐ ", "☑ ")
_NUMBERED_PREFIX_RE = re.compile(r"\d+\. ")
_WRITABLE_LINK_RE = re.compile(r"(https?://|mailto:)", re.IGNORECASE)
# How much of a block's rich_text a refusal quotes back as block_json.
_RICH_TEXT_QUOTE_LIMIT = 12_000
_MAX_NAMED_LOSSES = 5


def _normalized_runs(rich_text: list) -> list[tuple]:
    """*rich_text* as merged ``(text, style, link)`` runs, every annotation in the style.

    Whitespace at the edge of a run carries no marker or link, the way
    ``_rich_text_to_markdown`` writes it, so a run read back from its own
    Markdown compares equal.
    """
    def merged(runs: list[list], text: str, style: tuple, link: str | None) -> None:
        if not text:
            return
        if runs and runs[-1][1] == style and runs[-1][2] == link:
            runs[-1][0] += text
        else:
            runs.append([text, style, link])

    styled: list[list] = []
    for item in rich_text or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or "text"
        user_id = _mentioned_user_id(item)
        if kind == "text":
            text = (item.get("text") or {}).get("content") or item.get("plain_text", "")
        elif user_id:
            # A user mention reads and writes back as @[Name](user:<id>): the
            # person is what it carries, not the name Notion renders.
            text = f"user:{user_id}"
        else:
            text = item.get("plain_text", "")
        annotations = item.get("annotations") or {}
        flags = tuple(
            bool(annotations.get(flag))
            for flag in ("bold", "italic", "strikethrough", "code")
        )
        style = (
            kind,
            flags,
            bool(annotations.get("underline")),
            annotations.get("color") or "default",
        )
        link = _rich_text_link(item)
        merged(styled, text, style, _markdown_link_target(link) if link else None)

    runs: list[list] = []
    for text, style, link in styled:
        core = text.strip()
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :] if core else ""
        bare = (style[0], (False,) * len(style[1]), *style[2:])
        merged(runs, lead, bare, None)
        merged(runs, core, style, link)
        merged(runs, trail, bare, None)
    return [tuple(run) for run in runs]


def _mentioned_user_id(item: dict[str, object]) -> str | None:
    """The id of the person a user mention run names, else ``None``."""
    if item.get("type") != "mention":
        return None
    mention = item.get("mention")
    if not isinstance(mention, dict):
        return None
    user = mention.get("user")
    if mention.get("type") != "user" or not isinstance(user, dict):
        return None
    return str(user.get("id") or "") or None


def _quoted(text: str) -> str:
    text = " ".join(text.split())
    return f'"{text[:40]}..."' if len(text) > 40 else f'"{text}"'


def _markdown_losses(rich_text: list) -> list[str]:
    """What a Markdown rewrite of *rich_text* would lose, in words; ``[]`` when nothing."""
    rendered = _parse_inline_markdown(_rich_text_to_markdown(rich_text))
    if _normalized_runs(rendered) == _normalized_runs(rich_text):
        return []
    found: list[str] = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        quoted = _quoted(item.get("plain_text", ""))
        annotations = item.get("annotations") or {}
        if kind == "mention":
            sub = (item.get("mention") or {}).get("type") or ""
            found.append(f"a {sub} mention {quoted}".replace("  ", " "))
            continue
        if kind == "equation":
            found.append(f"an inline equation {quoted}")
            continue
        if annotations.get("underline"):
            found.append(f"underlined text {quoted}")
        color = annotations.get("color") or "default"
        if color != "default":
            found.append(f"{color.replace('_', ' ')} text {quoted}")
        link = _rich_text_link(item)
        if link and not _WRITABLE_LINK_RE.match(link):
            found.append(f"a link to {link}")
    found = list(dict.fromkeys(found))
    if not found:
        return [
            "characters the Markdown would read back as formatting "
            "(a literal *, _, `, ~~ or [label](url) in the text)"
        ]
    if len(found) > _MAX_NAMED_LOSSES:
        more = len(found) - _MAX_NAMED_LOSSES
        found = [*found[:_MAX_NAMED_LOSSES], f"{more} more"]
    return found


def _rich_text_request(rich_text: list) -> list[dict]:
    """*rich_text* as read, reduced to what a block update request carries."""
    out: list[dict] = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or "text"
        entry: dict = {"type": kind}
        if kind == "text":
            text = item.get("text") or {}
            body: dict = {"content": text.get("content", "")}
            if isinstance(text.get("link"), dict) and text["link"].get("url"):
                body["link"] = {"url": text["link"]["url"]}
            entry["text"] = body
        elif kind == "mention":
            mention = item.get("mention") or {}
            sub = mention.get("type")
            target = mention.get(sub) if sub else None
            if sub in ("user", "page", "database") and isinstance(target, dict):
                target = {"id": target.get("id")}
            entry["mention"] = {"type": sub, sub: target} if sub else mention
        else:
            entry[kind] = item.get(kind)
        annotations = {
            key: value
            for key, value in (item.get("annotations") or {}).items()
            if value not in (False, None, "default")
        }
        if annotations:
            entry["annotations"] = annotations
        out.append(entry)
    return out


def _text_links(rich_text: list) -> list[str]:
    """The URLs the text runs of *rich_text* link to, in order, once each."""
    links = (
        _rich_text_link(item)
        for item in rich_text or []
        if isinstance(item, dict) and (item.get("type") or "text") == "text"
    )
    return list(dict.fromkeys(_markdown_link_target(link) for link in links if link))


def _removed_links_note(before: list, after: list) -> str:
    kept = set(_text_links(after))
    removed = [link for link in _text_links(before) if link not in kept]
    if not removed:
        return ""
    return f" Links this update removed: {', '.join(removed)}."


def _strip_read_prefix(block_type: str, content: str) -> str:
    """*content* without the marker ``_block_to_text`` writes before this block's text."""
    if block_type == "to_do":
        for prefix in _TO_DO_PREFIXES:
            if content.startswith(prefix):
                return content[len(prefix) :]
        return content
    if block_type == "numbered_list_item":
        match = _NUMBERED_PREFIX_RE.match(content)
        return content[match.end() :] if match else content
    prefix = _TEXT_BLOCK_PREFIXES.get(block_type, "")
    if prefix and content.startswith(prefix):
        return content[len(prefix) :]
    return content


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    safe_request = ctx.request

    def _invalid_identifier_error(name: str, value: object) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=(
                f"Error: Invalid '{name}' argument: expected a string, got "
                f"{type(value).__name__}."
            ),
            is_error=True,
        )

    def _alignment_error(content: str) -> ToolResult:
        """Flag an alignment refusal as a tool ERROR, not a successful write.

        The block handlers otherwise return ``Error: ...`` strings, and the
        integration executor wraps a string with ``is_error=False`` (its
        override of ``CallableToolExecutor.execute_tool`` never received the
        classifier stamp). A refused write must land on the
        trace, the ``tool_done`` event and the per-tool error counters as the
        failure it is, exactly like ``upload_notion_file``'s ``_upload_error``
        already does for the same refusal.
        """
        return ToolResult(tool_call_id="", content=content, is_error=True)

    def _update_refusal(content: str) -> ToolResult:
        """A refused ``update_notion_block`` call, flagged as the error it is."""
        return ToolResult(tool_call_id="", content=content, is_error=True)

    def _resolve_block_identifier(
        block_id: object = None,
        page_id: object = None,
    ) -> tuple[str, ToolResult | None]:
        def _coerce_id(name: str, value: object) -> tuple[str, ToolResult | None]:
            if value is None:
                return "", None
            if not isinstance(value, str):
                return "", _invalid_identifier_error(name, value)
            return value.strip(), None

        block_id_value, block_id_err = _coerce_id("block_id", block_id)
        if block_id_err:
            return "", block_id_err

        page_id_value, page_id_err = _coerce_id("page_id", page_id)
        if page_id_err:
            return "", page_id_err

        if block_id_value and page_id_value and block_id_value != page_id_value:
            return "", ToolResult(
                tool_call_id="",
                content=(
                    "Error: Conflicting block identifiers. Provide either 'block_id' or "
                    "'page_id', or pass the same value for both."
                ),
                is_error=True,
            )

        target_id = block_id_value or page_id_value
        if not target_id:
            return "", ToolResult(
                tool_call_id="",
                content=(
                    "Error: Missing block identifier. Provide 'block_id' (preferred) "
                    "or 'page_id' (alias)."
                ),
                is_error=True,
            )
        return target_id, None

    async def get_notion_block(
        block_id: str | None = None,
        page_id: str | None = None,
    ) -> str | ToolResult:
        """Retrieve a single block by ID.

        ``block_id`` is the canonical parameter. ``page_id`` is accepted as a
        compatibility alias for model-generated tool calls.
        """
        target_id, target_err = _resolve_block_identifier(block_id, page_id)
        if target_err:
            return target_err

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/blocks/{target_id}",
                _PROVIDER,
                "retrieve block",
            )
            if err:
                return err
            block = resp.json()
            block_type = block.get("type", "unknown")
            text = _block_to_text(block) or "(no text content)"
            return (
                f"Block: {target_id}\n"
                f"Type: {block_type}\n"
                f"Has children: {block.get('has_children', False)}\n"
                f"Content: {text}"
            )

    async def _fetch_children_page(
        client, parent_id: str, count: int
    ) -> tuple[list[dict], str | None, bool]:
        """Up to *count* direct children of *parent_id*, paginated, and whether it has MORE."""
        blocks: list[dict] = []
        params: dict[str, str] = {"page_size": str(min(max(count, 1), 100))}
        more = False
        while len(blocks) < count:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/blocks/{parent_id}/children",
                _PROVIDER,
                "list block children",
                params=params,
            )
            if err:
                return blocks[:count], err, False
            data = resp.json()
            blocks.extend(data.get("results", []))
            more = bool(data.get("has_more") and data.get("next_cursor"))
            if not more:
                break
            params["start_cursor"] = data["next_cursor"]
        return blocks[:count], None, more or len(blocks) > count

    async def list_notion_block_children(
        block_id: str | None = None,
        page_id: str | None = None,
        count: int = 50,
        depth: int = 1,
    ) -> str | ToolResult:
        """Retrieve children blocks of a given block/page.

        ``block_id`` is the canonical parameter. ``page_id`` is accepted as a
        compatibility alias for model-generated tool calls. ``depth``
        (1..``_LIST_CHILDREN_MAX_DEPTH``) descends into the children of
        container blocks - a ``column_list`` page listed at depth 1 is a row
        of empty ``column`` containers, and each column then cost another
        call; at depth 3 the columns' content is listed under them, indented
        and numbered by path (``2.1.3.``). Blocks with children the depth did
        not reach keep their ``▸`` marker. Bounded three ways - levels,
        blocks listed and containers descended into
        (``_LIST_CHILDREN_MAX_FETCHES``: each is one more paginated fetch
        against Notion's rate limit); the descent awaits each container
        before the next, so one request is in flight at a time, and
        ``safe_request`` backs off on a 429. A listing a bound cut says so.
        """

        target_id, target_err = _resolve_block_identifier(block_id, page_id)
        if target_err:
            return target_err
        try:
            levels = max(1, min(int(depth if depth is not None else 1), _LIST_CHILDREN_MAX_DEPTH))
        except (TypeError, ValueError):
            levels = 1
        try:
            per_level = max(1, int(count if count is not None else 50))
        except (TypeError, ValueError):
            per_level = 50
        # One level never fetches more than the whole listing may show:
        # a huge ``count`` would otherwise page through Notion unboundedly
        # before the renderer's cap applied.
        per_level = min(per_level, _LIST_CHILDREN_MAX_BLOCKS)

        lines: list[str] = []
        rendered = 0
        fetches = 0
        truncated_at_cap = False
        stopped_descending = False
        # A parent with more children than ``count``: the listing says so
        # instead of reading as the whole page.
        count_cut = False

        async def _render(client, parent_id: str, level: int, prefix: str) -> str | None:
            nonlocal rendered, fetches, truncated_at_cap, stopped_descending, count_cut
            fetches += 1
            blocks, err, more = await _fetch_children_page(client, parent_id, per_level)
            count_cut = count_cut or more
            if err and not blocks:
                return err
            for i, block in enumerate(blocks, 1):
                if rendered >= _LIST_CHILDREN_MAX_BLOCKS:
                    truncated_at_cap = True
                    break
                rendered += 1
                block_type = block.get("type", "unknown")
                text = _block_to_text(block) or "(empty)"
                number = f"{prefix}{i}."
                indent = "  " * (level - 1)
                wants_children = bool(block.get("has_children")) and level < levels
                if wants_children and fetches >= _LIST_CHILDREN_MAX_FETCHES:
                    stopped_descending = True
                expanded = (
                    wants_children
                    and rendered < _LIST_CHILDREN_MAX_BLOCKS
                    and fetches < _LIST_CHILDREN_MAX_FETCHES
                )
                # ``▸`` marks children the listing does NOT descend into.
                marker = "▸" if block.get("has_children") and not expanded else " "
                lines.append(
                    f"{indent}{number} [{block_type}] {marker} {text}\n{indent}   ID: {block['id']}"
                )
                if expanded:
                    child_err = await _render(client, block["id"], level + 1, number)
                    if child_err:
                        lines.append(
                            f"{indent}   (children of {block['id']} unavailable: {child_err})"
                        )
            return err

        async with _client() as client:
            top_err = await _render(client, target_id, 1, "")
        if top_err and not lines:
            # The listing failed outright: a tool ERROR on the trace and the
            # counters, not a successful-looking string.
            return ToolResult(tool_call_id="", content=top_err, is_error=True)
        if not lines:
            return "No child blocks found."
        if top_err:
            # Some pages came back, a later one did not: say so, never a
            # complete-looking listing that silently stops short.
            lines.append(f"[Listing incomplete - a later page of children failed: {top_err}]")
        if truncated_at_cap:
            lines.append(
                f"[Listing cut at {_LIST_CHILDREN_MAX_BLOCKS} blocks - narrow the parent "
                "(pass a container's ID as block_id) or lower depth.]"
            )
        elif count_cut:
            lines.append(
                f"[More children than the {per_level} listed per level - raise count (max "
                f"{_LIST_CHILDREN_MAX_BLOCKS}) or pass a block's ID as block_id to list the rest.]"
            )
        if stopped_descending:
            lines.append(
                f"[Stopped descending after {_LIST_CHILDREN_MAX_FETCHES} containers - the blocks "
                "marked ▸ have unlisted children; pass one's ID as block_id to list them.]"
            )
        return "\n".join(lines)

    async def _append_children(
        client,
        block_id: str,
        children: list,
        after_block_id: str,
        action: str = "append blocks",
    ) -> tuple[int, str | None]:
        """Append ``children`` under ``block_id``: ``NotionContext.append_block_children``."""
        return await ctx.append_block_children(
            client, block_id, children, after_block_id=after_block_id, action=action
        )

    async def append_notion_blocks(
        block_id: str,
        content: str,
        after_block_id: str = "",
        alignment: str = "",
    ) -> str | ToolResult:
        """Append content blocks to a Notion page or block that supports children.

        ``block_id`` must be a **page ID** or a container block (toggle, column).
        Most leaf blocks (paragraphs, headings) do NOT support children.

        Supported markdown syntax:
          # / ## / ### Headings, - Bullets (indent to nest one level),
          1. Numbered, > Quotes, --- Dividers, [] / [x] To-dos, ``` fenced code
          blocks, and **markdown tables** (``| Col | Col |`` pipe syntax with
          optional separator rows) which become native Notion table blocks.
          Inline **bold**, *italic*, ``code``, ~~strike~~ and [label](url)
          become native rich-text annotations via :func:`_parse_inline_markdown`.

        ``after_block_id`` (optional): insert the new blocks directly AFTER that
        sibling block (e.g. under a heading) instead of at the end of the page.
        Find the block's ID with ``list_notion_block_children``.

        ``alignment`` is not in the schema and exists only to be refused
        (see ``_alignment_argument_refusal``)."""
        alignment_refusal = _alignment_argument_refusal(
            alignment, "the appended blocks", nothing_done=_NOTHING_APPENDED
        )
        if alignment_refusal:
            return _alignment_error(alignment_refusal)
        try:
            children = _text_to_blocks(content)
        except MarkdownBlockError as exc:
            return ToolResult(
                tool_call_id="", content=f"Error: {exc}; {_NOTHING_APPENDED}.", is_error=True
            )
        if not children:
            return "Error: No content to append. Provide text separated by double newlines."
        after_block_id = (after_block_id or "").strip()
        async with _client() as client:
            appended, err = await _append_children(
                client, block_id, children, after_block_id
            )
            if err:
                if appended:
                    return (
                        f"Appended {appended} of {len(children)} block(s) "
                        f"to {block_id}, then failed: {err}"
                    )
                return err
            suffix = f" after {after_block_id}" if after_block_id else ""
            return f"Appended {appended} block(s) to {block_id}{suffix}."

    async def append_notion_blocks_json(
        block_id: str,
        children_json: str,
        after_block_id: str = "",
        alignment: str = "",
    ) -> str | ToolResult:
        """Append raw Notion block objects (as JSON array) to a page or block.

        Use this for advanced block types (headings, lists, to-dos, code, etc.)
        that the simple text appender can't create.

        ``block_id`` must be a **page ID** or a block that supports children
        (e.g. toggle, column). Most leaf blocks (paragraphs, headings) do NOT
        support children — append to the parent page instead.

        ``after_block_id`` (optional): insert the new blocks directly AFTER that
        sibling block (e.g. under a heading) instead of at the end of the page.

        ``alignment`` is not in the schema and exists only to be refused
        (see ``_alignment_argument_refusal``); an ``alignment`` key
        INSIDE a block object is refused too (``_find_block_alignment``).

        IMPORTANT: ``annotations`` must be a sibling of ``text``, not nested inside it.
          Correct:   {"type": "text", "text": {"content": "Hi"}, "annotations": {"bold": true}}
          Wrong:     {"type": "text", "text": {"content": "Hi", "annotations": {"bold": true}}}

        Example: [{"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "My heading"}}]}}]
        """
        alignment_refusal = _alignment_argument_refusal(
            alignment, "the appended blocks", nothing_done=_NOTHING_APPENDED
        )
        if alignment_refusal:
            return _alignment_error(alignment_refusal)
        children, parse_err = _parse_json_arg(
            children_json, what="children_json", expected="array"
        )
        if parse_err:
            return parse_err
        if not isinstance(children, list) or not children:
            return "Error: Provide a non-empty JSON array of block objects."
        # Alignment guard: Notion has no API surface for a block's
        # alignment, so a child carrying the key can only produce a raw 400.
        # Refuse before any HTTP call, naming the exact key to drop.
        for idx, child in enumerate(children):
            aligned = _find_block_alignment(child)
            if aligned:
                return _alignment_error(
                    _alignment_refusal(
                        f"`children[{idx}].{aligned}` cannot be set", _ALIGNMENT_RETRY
                    )
                )
            null_icon = find_null_callout_icon(child)
            if null_icon:
                return _update_refusal(
                    f"Error: `children[{idx}].{null_icon}` is null: "
                    f"{CALLOUT_ICON_REMOVAL_REFUSAL}. Leave `icon` out of the "
                    f"payload; {_NOTHING_APPENDED}."
                )
        children = _sanitize_blocks(children)
        after_block_id = (after_block_id or "").strip()
        async with _client() as client:
            appended, err = await _append_children(
                client,
                block_id,
                children,
                after_block_id,
                action="append blocks (JSON)",
            )
            if err:
                if appended:
                    return (
                        f"Appended {appended} of {len(children)} block(s) "
                        f"to {block_id}, then failed: {err}"
                    )
                return err
            suffix = f" after {after_block_id}" if after_block_id else ""
            return f"Appended {appended} block(s) to {block_id}{suffix}."

    async def _update_block_text(block_id: str, content: str) -> str | ToolResult:
        """Rewrite a text block's text from inline Markdown, keeping what it did not change.

        The read tools show a block's text in the same Markdown
        (``_rich_text_to_markdown``), so an agent edits the words and every
        link and format it left alone is written back. A block holding what
        Markdown cannot write (a mention, an equation, underline, colour) is
        refused with its rich_text quoted as the block_json to edit instead -
        a Markdown rewrite would silently remove them.
        """
        async with _client() as client:
            block_resp, fetch_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/blocks/{block_id}",
                _PROVIDER,
                "retrieve block",
            )
            if fetch_err:
                return fetch_err
            block = block_resp.json()
            block_type = block.get("type") or ""
            if block_type not in _MARKDOWN_TEXT_BLOCKS:
                return _update_refusal(
                    f"Error: block {block_id} is a `{block_type or 'unknown'}` "
                    "block; `content` rewrites the text of paragraph, heading, "
                    "list item, quote, callout, toggle and to-do blocks only. "
                    f"Send `block_json` instead; {_NOTHING_UPDATED}."
                )
            live = (block.get(block_type) or {}).get("rich_text") or []
            losses = _markdown_losses(live)
            if losses:
                quoted = json.dumps(
                    {block_type: {"rich_text": _rich_text_request(live)}},
                    ensure_ascii=False,
                )
                if len(quoted) > _RICH_TEXT_QUOTE_LIMIT:
                    remedy = (
                        "Its rich_text is too long to quote here "
                        f"({len(quoted)} characters): change it in Notion."
                    )
                else:
                    remedy = (
                        "Edit its rich_text and send it as `block_json`:\n" + quoted
                    )
                return _update_refusal(
                    f"Error: block {block_id} holds {', '.join(losses)}, which "
                    "Markdown cannot write - a `content` update would remove "
                    f"them; {_NOTHING_UPDATED}. {remedy}"
                )
            try:
                text, fields = _update_block_fields(
                    block_type, _strip_read_prefix(block_type, content)
                )
            except MarkdownBlockError as exc:
                return _update_refusal(f"Error: {exc}; {_NOTHING_UPDATED}.")
            rich_text = _rich_text(text)
            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/blocks/{block_id}",
                _PROVIDER,
                "update block",
                retry_statuses=_BLOCK_WRITE_RETRY_STATUSES,
                json={block_type: {"rich_text": rich_text, **fields}},
            )
            if err:
                return err
            return f"Block {block_id} updated.{_removed_links_note(live, rich_text)}"

    async def update_notion_block(
        block_id: str,
        block_json: str = "",
        content: str | None = None,
        alignment: str = "",
    ) -> str | ToolResult:
        """Update a block's content: its text as Markdown, or the block type payload as JSON.

        ``content`` rewrites the text of a paragraph, heading, list item,
        quote, callout, toggle or to-do block from the inline Markdown the
        read tools show it in (see ``_update_block_text``).

        Only the block's *content* can be updated — Notion does NOT support
        changing a block's TYPE (e.g. ``paragraph`` → ``heading_3``). To
        convert a block, ``delete_notion_block`` it and ``append_notion_blocks_json``
        a new block of the desired type.

        Alignment (left / center / right) of an image or any other media block
        is a UI-only setting in Notion and cannot be set through the API:
        a payload carrying ``alignment`` is refused up front, before
        any HTTP call, with the remedy (the user aligns the block in Notion).
        The ``alignment`` ARGUMENT is not in the schema and exists only to be
        refused the same way (see ``_alignment_argument_refusal``).

        IMPORTANT: ``annotations`` must be a sibling of ``text``, not nested inside it.
          Correct:   {"type": "text", "text": {"content": "Hi"}, "annotations": {"bold": true}}
          Wrong:     {"type": "text", "text": {"content": "Hi", "annotations": {"bold": true}}}

        Example for paragraph: {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "Updated text"}}]}}
        Example for to_do: {"to_do": {"rich_text": [{"type": "text", "text": {"content": "Task"}}], "checked": true}}
        Example for table_row (IMPORTANT - cells must be arrays of rich_text objects, NOT bare strings):
          {"table_row": {"cells": [[{"type": "text", "text": {"content": "Alice"}}], [{"type": "text", "text": {"content": "Manager"}}]]}}
        """
        alignment_refusal = _alignment_argument_refusal(
            alignment, f"block {block_id}", nothing_done=_NOTHING_UPDATED
        )
        if alignment_refusal:
            return _alignment_error(alignment_refusal)
        if isinstance(content, str) and not content.strip():
            # A blank ``content`` is how a model fills an argument it does
            # not mean: it neither empties the block nor refuses the
            # ``block_json`` sent with it.
            content = None
        if content is not None:
            if block_json:
                return _update_refusal(
                    "Error: send either `content` (the block's new text, in "
                    "Markdown) or `block_json`, not both; "
                    f"{_NOTHING_UPDATED}."
                )
            if not isinstance(content, str):
                return _update_refusal(
                    "Error: `content` must be the block's new text as a "
                    f"string; {_NOTHING_UPDATED}."
                )
            return await _update_block_text(block_id, content)
        if not block_json:
            return _update_refusal(
                "Error: provide `content` (the block's new text, in Markdown) "
                f"or `block_json`; {_NOTHING_UPDATED}."
            )
        payload, parse_err = _parse_json_arg(
            block_json, what="block_json", expected="object"
        )
        if parse_err:
            return parse_err
        if not isinstance(payload, dict) or not payload:
            return "Error: block_json must be a non-empty JSON object."
        # Alignment guard: Notion has no API surface for a block's
        # alignment (the image / media request schemas are
        # ``additionalProperties: false``), so ``{"image": {"alignment":
        # "left"}}`` can only produce a raw 400 the agent retries. Refuse it
        # before the pre-flight GET and the PATCH - zero HTTP calls - naming
        # the exact key to drop so the rest of the payload is resent as-is.
        aligned = _find_block_alignment(payload)
        if aligned:
            return _alignment_error(_alignment_refusal(f"`{aligned}` cannot be set", _ALIGNMENT_RETRY))
        null_icon = find_null_callout_icon(payload)
        if null_icon:
            return _update_refusal(
                f"Error: `{null_icon}` is null: {CALLOUT_ICON_REMOVAL_REFUSAL}. "
                f"Leave `icon` out of the payload; {_NOTHING_UPDATED}."
            )
        _sanitize_blocks([payload])

        # Pre-flight type guard: Notion rejects a PATCH that carries
        # fields for a different block type than the live block with a raw
        # HTTP 400 that the agent tends to blindly retry. If the payload names
        # a block type, fetch the block first and refuse the type change up
        # front with actionable guidance. Only runs when the payload actually
        # contains a type key (a plain ``{"archived": true}`` skips it → no
        # extra latency).
        requested_types = {k for k in payload if k in _NOTION_BLOCK_TYPES}
        links_note = ""
        async with _client() as client:
            if requested_types:
                block_resp, fetch_err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/blocks/{block_id}",
                    _PROVIDER,
                    "retrieve block",
                )
                # If the pre-flight GET fails, fall through to the PATCH so we
                # never block a legitimate update on a transient read error —
                # the API stays the source of truth.
                if not fetch_err and block_resp is not None:
                    live_block = block_resp.json()
                    existing_type = live_block.get("type")
                    new_body = payload.get(existing_type)
                    if isinstance(new_body, dict) and isinstance(
                        new_body.get("rich_text"), list
                    ):
                        links_note = _removed_links_note(
                            (live_block.get(existing_type) or {}).get("rich_text"),
                            new_body["rich_text"],
                        )
                    # Refuse if ANY requested type key differs from the live
                    # block type — including a payload that mixes the matching
                    # type with another (e.g. ``paragraph`` + ``heading_3``),
                    # which would otherwise slip past a plain membership check
                    # and still hit Notion's block-type-mismatch 400.
                    mismatched = (
                        sorted(requested_types - {existing_type})
                        if existing_type
                        else []
                    )
                    if mismatched:
                        requested = ", ".join(f"`{t}`" for t in mismatched)
                        return (
                            f"Error: cannot change the type of block {block_id}. "
                            f"It is a `{existing_type}` block, but the update "
                            f"includes fields for {requested}, and Notion does "
                            "not support changing a block's type. To change the "
                            "type, `delete_notion_block` this block and create a "
                            "new block of the desired type with "
                            "`append_notion_blocks_json` (it is added to the end "
                            "of the parent, not at this block's position). To "
                            f"edit this block instead, send a `{existing_type}` "
                            "payload."
                        )

            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/blocks/{block_id}",
                _PROVIDER,
                "update block",
                retry_statuses=_BLOCK_WRITE_RETRY_STATUSES,
                json=payload,
            )
            if err:
                return err
            return f"Block {block_id} updated.{links_note}"

    async def delete_notion_block(block_id: str) -> str:
        """Delete (trash) a block. Can be a block inside a page, or a page block itself."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/blocks/{block_id}",
                _PROVIDER,
                "delete block",
            )
            if err:
                return err
            return f"Block {block_id} deleted (moved to trash)."

    executor.register("get_notion_block", get_notion_block)
    executor.register("list_notion_block_children", list_notion_block_children)
    executor.register("append_notion_blocks", append_notion_blocks)
    executor.register("append_notion_blocks_json", append_notion_blocks_json)
    executor.register("update_notion_block", update_notion_block)
    executor.register("delete_notion_block", delete_notion_block)

    return [
        # ── Blocks ─────────────────────────────────────────────────────
        ToolDefinition(
            name="get_notion_block",
            description=(
                "Retrieve a single Notion block by ID. Returns its type, content, "
                "and whether it has children. Text is shown as inline Markdown: "
                "links as [label](url), **bold**, *italic*, `code`, ~~strikethrough~~."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "string",
                        "description": "Block ID to retrieve (preferred parameter name)",
                    },
                    "page_id": {
                        "type": "string",
                        "description": "Alias for block_id (accepted for compatibility)",
                    },
                },
                "required": [],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="list_notion_block_children",
            description=(
                "List children blocks of a page or block. Returns block types, "
                "content (text as inline Markdown, links as [label](url), a "
                "block's colour as {color=...}, a callout as "
                "> [!icon=... color=...]), and "
                "IDs for further operations. Pass depth=2 or 3 to "
                "list the content INSIDE container blocks (column_list, column, "
                "toggle, callout) in the same call, indented under them - a "
                "depth-1 listing of a column layout is a row of empty columns."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "string",
                        "description": "Parent block or page ID (preferred parameter name)",
                    },
                    "page_id": {
                        "type": "string",
                        "description": "Alias for block_id (accepted for compatibility)",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _LIST_CHILDREN_MAX_BLOCKS,
                        "description": (
                            "Max blocks to return per level (default 50, "
                            f"max {_LIST_CHILDREN_MAX_BLOCKS})"
                        ),
                        "default": 50,
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _LIST_CHILDREN_MAX_DEPTH,
                        "description": (
                            "How many levels of children to descend into (default 1: "
                            f"direct children only; up to {_LIST_CHILDREN_MAX_DEPTH}). "
                            f"At most {_LIST_CHILDREN_MAX_BLOCKS} blocks are listed."
                        ),
                        "default": 1,
                    },
                },
                "required": [],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="append_notion_blocks",
            priority=0,
            description=(
                "Append content blocks to a Notion page. The content field "
                "accepts rich Markdown converted to native Notion blocks: "
                "# to #### headings, - / * / + bullets (indent to nest one "
                "level), 1. / 1) numbered lists, [] / [x] to-dos, > quotes, "
                "--- dividers, | a | b | tables and ``` fenced code blocks. "
                "Inline **bold**, *italic*, `code`, ~~strikethrough~~ and "
                "[label](https://url) render as real formatting - write Markdown "
                "directly, never escape the markers. "
                f"{MARKDOWN_LAYOUT_SYNTAX} One call builds the whole layout, "
                "however deeply it nests."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "string",
                        "description": "Page or block ID to append to",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown content. Block syntax: # to #### headings, "
                            "- / * / + bullets (indent to nest), 1. / 1) numbered, "
                            "[] / [x] to-dos, > quote, --- divider, | a | b | table, "
                            "``` code fence, > [!icon=... color=...] callout, "
                            "{color=...} suffix, ::: columns / ::: column / ::: "
                            "layout. Inline: **bold**, *italic*, `code`, "
                            "~~strikethrough~~, [label](https://url)."
                        ),
                    },
                    "after_block_id": {
                        "type": "string",
                        "description": (
                            "Optional. Insert the new blocks directly AFTER this "
                            "sibling block (e.g. a heading) instead of at the end "
                            "of the page. Find the ID with "
                            "list_notion_block_children."
                        ),
                    },
                },
                "required": ["block_id", "content"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="append_notion_blocks_json",
            description=(
                "Append raw Notion block objects (JSON array) to a page or block. "
                "Use for advanced blocks: headings, lists, to-dos, code blocks, etc. "
                "Children nested deeper than Notion takes in one request (a "
                "callout inside a column) are appended under the blocks just "
                "created. An empty table cell is []. A callout always gets "
                "an icon (Notion's default when none is sent). Example: "
                '[{"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Title"}}]}}]'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {
                        "type": "string",
                        "description": "Page or block ID to append to",
                    },
                    "children_json": {
                        "type": "string",
                        "description": "JSON array of Notion block objects",
                    },
                    "after_block_id": {
                        "type": "string",
                        "description": (
                            "Optional. Insert the new blocks directly AFTER this "
                            "sibling block (e.g. a heading) instead of at the end "
                            "of the page."
                        ),
                    },
                },
                "required": ["block_id", "children_json"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="update_notion_block",
            description=(
                "Update a block's CONTENT. To change the text of a paragraph, "
                "heading, list item, quote, callout, toggle or to-do, pass "
                "`content`: the text as the read tools show it (inline Markdown, "
                "links as [label](url)) with your edit - every link and format "
                "you leave in place is kept. A trailing {color=...} sets the "
                "block's colour, and a callout's `> [!icon=... color=...]` "
                "marker its icon and colour (Notion cannot remove a callout "
                "icon through the API). Otherwise provide the block type "
                "payload as `block_json`. "
                'Example: {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "Updated"}}]}} '
                'or {"to_do": {"checked": true}}. '
                "For table_row blocks, cells MUST be arrays of rich_text objects, NOT bare strings: "
                '{"table_row": {"cells": [[{"type": "text", "text": {"content": "Alice"}}], [{"type": "text", "text": {"content": "Manager"}}]]}}. '
                "Notion does NOT support changing a block's TYPE (e.g. paragraph "
                "-> heading_3): the payload's type must match the existing block. "
                "To convert a block, delete_notion_block it and "
                "append_notion_blocks_json a new block of the desired type. "
                "Image/media block alignment (left/center/right) is a UI-only "
                "setting in Notion and cannot be set through the API: never send "
                "an alignment key - tell the user to align the block in Notion "
                "(hover the block > alignment icon)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {"type": "string", "description": "Block ID to update"},
                    "content": {
                        "type": "string",
                        "description": (
                            "The block's new text in inline Markdown: **bold**, "
                            "*italic*, `code`, ~~strikethrough~~, [label](https://url). "
                            "Use instead of block_json for a text edit."
                        ),
                    },
                    "block_json": {
                        "type": "string",
                        "description": "Block update payload as JSON (when not using content)",
                    },
                },
                "required": ["block_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="delete_notion_block",
            description="Delete (trash) a Notion block. Can be a content block or a page block.",
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {"type": "string", "description": "Block ID to delete"},
                },
                "required": ["block_id"],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
