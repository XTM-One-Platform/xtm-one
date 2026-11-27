"""Notion page tools: read, properties, create, update, move, trash."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote

from app.core.utils import is_valid_uuid
from app.tools.base import ToolDefinition, ToolResult

from ._common import (
    _BASE_URL,
    _DATABASE_ROW_PARENT_TYPES,
    _NOTION_BLOCK_LIMIT,
    _PROVIDER,
    _READ_PAGE_MAX_CHARS,
    _READ_PAGE_WINDOW_MAX_CHARS,
    NotionContext,
    _block_to_text,
    _extract_title,
    _is_database_not_page_400,
    _is_page_id_400,
    _names_existing_resource,
    _parse_json_arg,
    _split_request_nesting,
    logger,
)
from .formatting import (
    MARKDOWN_LAYOUT_SYNTAX,
    MarkdownBlockError,
    _list_empty_properties,
    _summarize_properties,
    _text_to_blocks,
    _truncate_properties_rich_text,
)

# LLMs routinely pass natural-language placement words instead of the two
# documented sentinels — most notably the literal string "bottom", which the
# old code forwarded verbatim as ``position.after_block.id`` and Notion
# rejected with a 400 "should be a valid uuid, instead was `\"bottom\"`".
# Map every plausible alias deterministically: end-aliases
# mean "omit position" (Notion appends at the end by default), start-aliases
# mean ``page_start``.
_POSITION_START_ALIASES = frozenset({"start", "top", "page_start", "beginning", "first"})
_POSITION_END_ALIASES = frozenset(
    {"", "end", "bottom", "last", "page_end", "default", "append", "none", "null"}
)


def _resolve_position(
    position: str | int | float | bool | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Map a ``position`` tool argument to a Notion ``position`` payload.

    Returns ``(payload, error)``:
      * ``(None, None)`` — omit the ``position`` field entirely (append at
        the end of the parent, Notion's default behaviour);
      * ``({"type": "page_start"}, None)`` — insert at the top;
      * ``({"type": "after_block", ...}, None)`` — insert after a block;
      * ``(None, error_string)`` — the value is neither a known alias nor a
        valid block UUID. Fail fast with actionable guidance instead of
        letting Notion reject the whole request with an opaque 400.
    """
    # Tool arguments are not JSON-schema coerced: an LLM can emit a
    # non-string (int, bool, null). Coerce defensively so a bad type still
    # reaches the actionable fail-fast below instead of raising
    # AttributeError on .strip() and surfacing a generic tool exception.
    value = str(position).strip() if position is not None else ""
    lowered = value.lower()
    if lowered in _POSITION_END_ALIASES:
        return None, None
    if lowered in _POSITION_START_ALIASES:
        return {"type": "page_start"}, None
    if not is_valid_uuid(value):
        return None, (
            f"Error: position '{value}' is not valid. Use 'start' to insert "
            "at the top of the parent, omit it (or pass 'end'/'bottom') to "
            "append at the bottom, or pass the UUID of an existing sibling "
            "block to insert after (find block IDs with "
            "list_notion_block_children)."
        )
    return {"type": "after_block", "after_block": {"id": value}}, None


def _coerce_int(value: object, default: int) -> int:
    """A tool argument as an int; *default* for anything that is not one."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _window_page_content(
    full_content: str,
    *,
    title: str,
    offset: int | None = 0,
    max_chars: int | None = None,
) -> str | ToolResult:
    """One window of a rendered page, cut at a line boundary.

    ``offset`` is a character position into the rendered page (0 = the
    start), ``max_chars`` the window size - the default
    ``_READ_PAGE_MAX_CHARS`` keeps the one-call shape of a short page, the
    cap ``_READ_PAGE_WINDOW_MAX_CHARS`` bounds a deliberate wide read. A
    window that does not reach the end is cut at the last line break in its
    second half (so a bullet is never split) and ends with the offset to
    continue from; an offset past the end is a tool ERROR saying so,
    never an empty string the model would read as
    an empty page.
    """
    total = len(full_content)
    start = max(0, _coerce_int(offset, 0))
    window = _coerce_int(max_chars, _READ_PAGE_MAX_CHARS)
    if window < 1:
        window = _READ_PAGE_MAX_CHARS
    window = min(window, _READ_PAGE_WINDOW_MAX_CHARS)
    if start == 0 and total <= window:
        return full_content
    if start >= total:
        return ToolResult(
            tool_call_id="",
            content=(
                f"Error: offset {start:,} is past the end of the page '{title}' "
                f"({total:,} characters). Use an offset below {total:,}."
            ),
            is_error=True,
        )
    end = min(start + window, total)
    if end < total:
        cut = full_content.rfind("\n", start + window // 2, end)
        if cut > start:
            end = cut
    body = full_content[start:end]
    header = f"# {title} (characters {start:,}-{end:,} of {total:,})\n" if start > 0 else ""
    footer = (
        f"\n\n[{total - end:,} characters remain. Call get_notion_page again with "
        f"offset={end} to continue.]"
        if end < total
        else ""
    )
    return f"{header}{body}{footer}"


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    safe_request = ctx.request
    _fetch_all_blocks = ctx.fetch_all_blocks
    _resolve_relation_titles = ctx.resolve_relation_titles

    async def _verify_page_parent(client, page_id: str) -> None:
        """Read the page a create / move falls back to once both type probes missed.

        A probe miss says "not a database / data source", not "gone" - and it
        feeds the stale-reference detector. The page's own GET is the
        one call whose answer settles it: found, the parent is proven alive
        (and any report withdrawn) even if the write then fails for another
        reason; missing, the parent really is a stale reference.
        """
        await safe_request(client, "get", f"{_BASE_URL}/pages/{page_id}", _PROVIDER, "verify parent page")

    def _database_not_page_error(page_id: str, tool: str) -> str:
        """Actionable redirect when a database ID reaches a page read tool.

        Mirror of the "is a page, not a database" handling in
        ``databases.py``: Notion 400s with "is a database, not
        a page" when a database ID is passed to ``GET /v1/pages/{id}``.
        Without this, the raw 400 triggered a cascade of wasted follow-up
        calls in production (2026-07-18 log triage) — the agent
        got no hint about WHICH tool to use instead.
        """
        return (
            f"Error: '{page_id}' is a Notion database, not a page. "
            "Use get_notion_database to inspect its schema or "
            "query_notion_database to list its entries instead of "
            f"{tool}."
        )

    async def get_notion_page(
        page_id: str,
        offset: int = 0,
        max_chars: int | None = None,
    ) -> str | ToolResult:
        """Read the content of a Notion page (all text blocks, recursively).

        Windowed: ``max_chars`` characters (default
        ``_READ_PAGE_MAX_CHARS``, at most ``_READ_PAGE_WINDOW_MAX_CHARS``)
        from ``offset``, cut at a line boundary, and a longer page says at
        which offset to continue - the second half of a long strategy page
        used to be unreachable in any number of calls. Also includes
        database properties (metadata) for pages inside databases.
        """
        logger.info("get_notion_page: page_id=%s offset=%s", page_id, offset)
        async with _client() as client:
            # ``expected_statuses={400}`` keeps the benign daily
            # database-ID-in-a-page-tool 400 out of the warning stream and
            # opts into receiving the failed response so it can be diagnosed.
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "read page",
                expected_statuses={400},
            )
            if err:
                if _is_database_not_page_400(meta_resp):
                    logger.info(
                        "get_notion_page: %s is a database, not a page — "
                        "redirecting the agent to the database tools",
                        page_id,
                    )
                    return _database_not_page_error(page_id, "get_notion_page")
                if meta_resp is not None and meta_resp.status_code == 400:
                    # Any OTHER 400 is unexpected here — surface it explicitly
                    # since expected_statuses={400} suppressed the standard
                    # safe_request warning (mirrors resolve_data_source_id).
                    logger.warning(
                        "get_notion_page: unexpected 400 for %s "
                        "(not the database-not-page case): %s",
                        page_id,
                        err,
                    )
                    return err
                logger.warning("get_notion_page: error for page %s: %s", page_id, err)
                return err
            meta = meta_resp.json()
            title = _extract_title(meta)

            # Include database properties (metadata) for pages inside databases
            parent = meta.get("parent", {})
            parent_type = parent.get("type", "")
            logger.info(
                "get_notion_page: title=%r parent_type=%s page_id=%s",
                title,
                parent_type,
                page_id,
            )

            content_lines = [f"# {title}\n"]

            # If this page is inside a database, show its metadata properties
            if parent_type in _DATABASE_ROW_PARENT_TYPES:
                props = meta.get("properties", {})
                if props:
                    # Resolve relation page IDs → titles for readability
                    resolved = await _resolve_relation_titles(client, props)
                    prop_summary = _summarize_properties(
                        props, resolved_relations=resolved
                    )
                    if prop_summary and prop_summary != "No properties":
                        content_lines.append(f"**Properties:** {prop_summary}\n")
                    # Surface empty/unset properties so the agent knows they exist
                    empty = _list_empty_properties(props)
                    if empty:
                        content_lines.append(f"**Unset properties:** {empty}\n")
                    logger.debug(
                        "get_notion_page: included %d properties for DB page %s (resolved %d relations, empty: %s)",
                        len(props),
                        page_id,
                        len(resolved),
                        empty or "none",
                    )

            blocks = await _fetch_all_blocks(client, page_id)
            logger.info(
                "get_notion_page: fetched %d blocks for page %s",
                len(blocks),
                page_id,
            )

            for block in blocks:
                depth = block.get("_depth", 0)
                text = _block_to_text(block)
                if text:
                    indent = "  " * depth
                    content_lines.append(f"{indent}{text}")

            full_content = _window_page_content(
                "\n".join(content_lines), title=title, offset=offset, max_chars=max_chars
            )
            logger.info(
                "get_notion_page: returning %d chars for page %s (%s)",
                len(full_content.content if isinstance(full_content, ToolResult) else full_content),
                page_id,
                title,
            )
            return full_content

    async def get_notion_page_properties(page_id: str) -> str:
        """Retrieve all database properties (metadata) for a Notion page.

        Returns a detailed, structured view of every property on the page —
        including relation targets (resolved to page titles), rollups,
        and unset/empty properties.  Use this when you need to inspect or
        verify database-level metadata without reading the full page content.
        """
        logger.info("get_notion_page_properties: page_id=%s", page_id)
        async with _client() as client:
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "get page properties",
                expected_statuses={400},
            )
            if err:
                if _is_database_not_page_400(meta_resp):
                    return _database_not_page_error(
                        page_id, "get_notion_page_properties"
                    )
                if meta_resp is not None and meta_resp.status_code == 400:
                    logger.warning(
                        "get_notion_page_properties: unexpected 400 for %s "
                        "(not the database-not-page case): %s",
                        page_id,
                        err,
                    )
                return err
            meta = meta_resp.json()
            title = _extract_title(meta)

            parent = meta.get("parent", {})
            parent_type = parent.get("type", "")

            if parent_type not in _DATABASE_ROW_PARENT_TYPES:
                return (
                    f"Page '{title}' is not inside a database "
                    f"(parent type: {parent_type}). "
                    "Database properties are only available for pages "
                    "that belong to a Notion database."
                )

            props = meta.get("properties", {})
            if not props:
                return f"Page '{title}' has no properties."

            # Resolve relation page titles
            resolved = await _resolve_relation_titles(client, props)

            # Build a detailed per-property report
            lines = [f"# Properties for: {title}\n"]
            for name, prop in props.items():
                prop_type = prop.get("type", "")
                prop_id = prop.get("id", "")
                id_tag = f" [id={prop_id}]" if prop_id else ""
                # Re-use the summarizer for the value with resolved relations
                single_summary = _summarize_properties(
                    {name: prop},
                    resolved_relations=resolved,
                )
                if single_summary == "No properties" or not single_summary:
                    lines.append(f"- **{name}** ({prop_type}){id_tag}: _(empty)_")
                else:
                    # single_summary is "Name: value" — strip the "Name: " prefix
                    val = (
                        single_summary.split(": ", 1)[1]
                        if ": " in single_summary
                        else single_summary
                    )
                    lines.append(f"- **{name}** ({prop_type}){id_tag}: {val}")

            # Footer with empty property list
            empty = _list_empty_properties(props)
            if empty:
                lines.append(f"\n**Unset properties that need attention:** {empty}")

            result = "\n".join(lines)
            logger.info(
                "get_notion_page_properties: returning %d chars for %s (%s)",
                len(result),
                page_id,
                title,
            )
            return result

    async def create_notion_page(
        parent_id: str,
        title: str,
        content: str = "",
        position: str = "",
        icon_emoji: str = "",
        cover_url: str = "",
        properties_json: str | dict[str, Any] | None = None,
        template_id: str = "",
    ) -> str | ToolResult:
        """Create a new page in Notion under a parent page, database, or data source.

        ``parent_id`` can be a page ID, database ID, or data_source ID.
        The type is auto-detected. For databases, the ID is resolved to a
        data_source_id (API 2025-09-03).

        ``content`` supports markdown-like formatting (see ``append_notion_blocks``).
        ``position`` (optional, page parents only): 'start' to insert at the
          top, 'end'/'bottom' or omitted to append at the bottom (default),
          or the UUID of an existing sibling block to insert after.
        ``icon_emoji`` (optional): an emoji character for the page icon.
        ``cover_url`` (optional): an external URL for the page cover image.
        ``properties_json`` (optional): JSON string or object with additional
          property values for database parents. Merged with the auto-generated
          title property. Example:
          {"Status": {"status": {"name": "In Progress"}}}
        ``template_id`` (optional): ID of a database template to apply. Use
          ``list_notion_templates`` to discover available templates. When set,
          ``content`` is ignored (Notion populates the page from the template
          asynchronously). Use 'default' to apply the data source's default
          template.
        """
        logger.info("create_notion_page: parent=%s title=%r", parent_id, title)

        # Fail-fast on malformed / wrong-shaped ``properties_json`` BEFORE
        # opening the client and burning the 1–2 Notion API calls the
        # parent auto-detect costs — otherwise an LLM passing a bad
        # ``properties_json`` retries the parent-detect round-trips on
        # every attempt before getting back the deterministic parse error,
        # exactly the wasted-API-calls pattern this incident fix is
        # trying to remove.  Mirrors the order ``update_notion_page`` and
        # ``query_notion_database`` already use for their JSON args.
        extra_props: dict | None = None
        if properties_json is not None and properties_json != "":
            parsed_props, parse_err = _parse_json_arg(
                properties_json, what="properties_json", expected="object"
            )
            if parse_err:
                return parse_err
            # ``_parse_json_arg`` accepts dict / list / str-encoded; we
            # need a dict here. A valid-but-wrong shape (e.g. a list)
            # would be forwarded to Notion as ``body["properties"] =
            # {...title, **dict_or_silently_ignored}`` and either:
            #   * silently drop the caller's intent (current bug, the
            #     update succeeds without their custom properties), or
            #   * trigger a 400 from Notion if we let it through
            #     unchecked.
            # Return an actionable error instead so the LLM can
            # retry with the correct shape.
            if not isinstance(parsed_props, dict):
                return (
                    "Error: properties_json must be a JSON object "
                    "(dict) keyed by Notion property name, not a "
                    f"{type(parsed_props).__name__}."
                )
            extra_props = parsed_props

        # Fail-fast on a bad ``position`` for the same reason: an
        # unrecognised value (e.g. the literal "bottom") used
        # to be forwarded as ``position.after_block.id`` and rejected by
        # Notion with a 400 AFTER the parent-detect round-trips.
        position_payload, position_err = _resolve_position(position)
        if position_err:
            return position_err

        # And on a layout directive the converter cannot honour.
        use_template = bool(template_id)
        all_children: list[dict] = []
        if not use_template and content:
            try:
                all_children = _text_to_blocks(content)
            except MarkdownBlockError as exc:
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: {exc}; nothing has been created.",
                    is_error=True,
                )

        parent: dict
        properties: dict
        async with _client() as client:
            # Auto-detect parent type: database/data_source vs page
            check, check_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/databases/{parent_id}",
                _PROVIDER,
                "detect parent type",
            )
            if check is not None and check.status_code == 200:
                db_data = check.json()
                data_sources = db_data.get("data_sources", [])
                if data_sources:
                    # 2025-09-03: use data_source_id as parent
                    ds_id = data_sources[0]["id"]
                    parent = {"type": "data_source_id", "data_source_id": ds_id}
                    logger.info(
                        "create_notion_page: resolved database %s → data_source %s",
                        parent_id,
                        ds_id,
                    )
                else:
                    parent = {"database_id": parent_id}
                    logger.debug("create_notion_page: parent is a database (legacy)")
                properties = {
                    "title": {"title": [{"type": "text", "text": {"content": title}}]}
                }
            else:
                # Try as data_source_id directly
                ds_check, _ = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/data_sources/{parent_id}",
                    _PROVIDER,
                    "check data source",
                )
                if ds_check is not None:
                    parent = {"type": "data_source_id", "data_source_id": parent_id}
                    logger.debug("create_notion_page: parent is a data_source")
                else:
                    parent = {"page_id": parent_id}
                    logger.debug("create_notion_page: parent is a page")
                    if not _names_existing_resource(None, check_err):
                        await _verify_page_parent(client, parent_id)
                properties = {
                    "title": {"title": [{"type": "text", "text": {"content": title}}]}
                }

            # ``extra_props`` was parsed + shape-validated above so a bad
            # input fails fast without paying the parent-detect API
            # calls.  An empty ``{}`` decodes to "no extra properties"
            # (same observable behaviour as passing ``None``), so we
            # only merge on non-empty.
            if extra_props:
                properties.update(extra_props)

            # Truncate AFTER the merge so both the literal title built above
            # and any rich_text/title fields supplied via properties_json are
            # capped to Notion's 2 000-char per-item limit.  Calling this
            # unconditionally (not just when properties_json is non-empty)
            # is the key difference from the previous version: a >2 000-char
            # ``title`` argument alone — with no properties_json — was
            # silently producing an HTTP 400 before.
            _truncate_properties_rich_text(properties)

            body: dict = {
                "parent": parent,
                "properties": properties,
            }

            # What the create request cannot carry - blocks past the first
            # ``_NOTION_BLOCK_LIMIT``, children nested too deep - is appended
            # once the page exists.
            nested: list = []
            if not use_template:
                body["children"], nested = _split_request_nesting(
                    all_children[:_NOTION_BLOCK_LIMIT]
                )

            if template_id:
                if template_id.lower() == "default":
                    body["template"] = {"type": "default"}
                else:
                    body["template"] = {
                        "type": "template_id",
                        "template_id": template_id,
                    }

            if position_payload:
                body["position"] = position_payload

            if icon_emoji:
                body["icon"] = {"type": "emoji", "emoji": icon_emoji}
            if cover_url:
                body["cover"] = {"type": "external", "external": {"url": cover_url}}

            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/pages",
                _PROVIDER,
                "create page",
                json=body,
            )
            if err:
                logger.warning("create_notion_page: error: %s", err)
                return err
            # A page parent misses both type probes above on every run (the
            # data-source probe 404s): the create proves it exists.
            ctx.note_resource_alive(parent_id)
            page = resp.json()
            page_id = page["id"]

            if not use_template:
                if nested:
                    nested_err = await ctx.append_deferred_children(
                        client, nested, parent_id=page_id, action="add nested blocks"
                    )
                    if nested_err:
                        logger.warning(
                            "create_notion_page: page created but nested blocks failed: %s",
                            nested_err,
                        )
                        return (
                            f"Page created: {page.get('url', '')} (ID: {page_id}) "
                            f"— but some nested blocks were not added: {nested_err}"
                        )
                overflow = all_children[_NOTION_BLOCK_LIMIT:]
                if overflow:
                    appended, append_err = await ctx.append_block_children(
                        client, page_id, overflow, action="append overflow blocks"
                    )
                    if append_err:
                        logger.warning(
                            "create_notion_page: page created but append failed: %s",
                            append_err,
                        )
                        return (
                            f"Page created: {page.get('url', '')} (ID: {page_id}) "
                            f"— but only {_NOTION_BLOCK_LIMIT + appended} of "
                            f"{len(all_children)} blocks were added: {append_err}"
                        )

            logger.info(
                "create_notion_page: created %s (%s)", page_id, page.get("url", "")
            )
            tmpl_note = (
                " (template applied asynchronously — page content may take a moment to populate)"
                if use_template
                else ""
            )
            return f"Page created: {page.get('url', '')} (ID: {page_id}){tmpl_note}"

    async def update_notion_page(
        page_id: str,
        properties_json: str | dict[str, Any] | None = None,
        icon_emoji: str = "",
        cover_url: str = "",
        is_locked: str = "",
        template_id: str = "",
        erase_content: str = "",
    ) -> str:
        """Update a Notion page: properties, icon, cover, lock status, and/or apply a template.

        ``properties_json``: JSON string or object with property updates.
          Example for select: {"Priority": {"select": {"name": "High"}}}
          Example for status: {"Status": {"status": {"name": "Done"}}}
          IMPORTANT: "status" and "select" are different Notion property types.
          Use {"status": {"name": "..."}} for Status properties, not "select".
        ``icon_emoji``: set an emoji icon (e.g. "📄"). Pass "null" to remove.
        ``cover_url``: set an external cover image URL. Pass "null" to remove.
        ``is_locked``: "true" to lock page from UI editing, "false" to unlock.
        ``template_id`` (optional): apply a template to the page. Use a template
          ID from ``list_notion_templates``, or 'default' for the data source's
          default template. Template content is appended to existing content
          unless ``erase_content`` is 'true'.
        ``erase_content`` (optional): 'true' to replace existing page content
          with the template content (destructive, cannot be undone via API).
          Only used when ``template_id`` is set.
        """
        logger.info("update_notion_page: page_id=%s", page_id)
        body: dict = {}
        # Symmetric with ``create_notion_page``: ``properties_json`` defaults
        # to ``None`` and an empty ``{}`` / empty string ``""`` short-circuits
        # the entire block (semantically equivalent to "no property updates
        # requested").  Same error messages as ``create_notion_page`` so a
        # caller switching between the two never sees inconsistent wording.
        if properties_json:
            parsed_props, parse_err = _parse_json_arg(
                properties_json, what="properties_json", expected="object"
            )
            if parse_err:
                return parse_err
            # Notion's PATCH /pages/{id} expects ``properties`` as a JSON
            # object keyed by property name. A list / scalar would
            # produce an opaque 400 from Notion that the LLM would then
            # retry, restarting the failure cascade this PR is fixing.
            if not isinstance(parsed_props, dict):
                return (
                    "Error: properties_json must be a JSON object "
                    "(dict) keyed by Notion property name, not a "
                    f"{type(parsed_props).__name__}."
                )
            # Cap rich_text/title content at Notion's 2 000-char per-item
            # limit before sending — same protection ``create_notion_page``
            # applies via ``_truncate_properties_rich_text``, kept here
            # so a long ``Handoff Summary`` reaches the upstream
            # truncated rather than as an opaque 400.
            _truncate_properties_rich_text(parsed_props)
            body["properties"] = parsed_props
        if icon_emoji:
            if icon_emoji == "null":
                body["icon"] = None
            else:
                body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        if cover_url:
            if cover_url == "null":
                body["cover"] = None
            else:
                body["cover"] = {"type": "external", "external": {"url": cover_url}}
        if is_locked:
            body["is_locked"] = is_locked.lower() == "true"
        if template_id:
            if template_id.lower() == "default":
                body["template"] = {"type": "default"}
            else:
                body["template"] = {
                    "type": "template_id",
                    "template_id": template_id,
                }
            if erase_content.lower() == "true":
                body["erase_content"] = True

        if not body:
            return (
                "Error: Provide at least one of properties_json, icon_emoji, "
                "cover_url, is_locked, or template_id."
            )

        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "update page",
                json=body,
            )
            if err:
                logger.warning("update_notion_page: error for %s: %s", page_id, err)
                return err
            updated = []
            if "properties" in body:
                updated.append("properties")
            if "icon" in body:
                updated.append("icon")
            if "cover" in body:
                updated.append("cover")
            if "is_locked" in body:
                updated.append("lock status")
            if "template" in body:
                updated.append("template")
            logger.info(
                "update_notion_page: updated %s (%s)", page_id, ", ".join(updated)
            )
            tmpl_note = (
                " — template applied asynchronously, content may take a moment to populate"
                if "template" in body
                else ""
            )
            return f"Page {page_id} updated ({', '.join(updated)}){tmpl_note}."

    async def move_notion_page(
        page_id: str, new_parent_id: str, position: str = ""
    ) -> str:
        """Move a Notion page to a different parent page or data source.

        ``new_parent_id``: the UUID of the new parent page, database, or
        data source (databases are auto-resolved to their data source).
        Moving to the workspace root is NOT supported by the Notion API.
        ``position`` (optional): 'start' to place at top, 'end'/'bottom' or
        omitted for the bottom of the new parent (default), or the UUID of
        a sibling block to insert after.
        """
        logger.info(
            "move_notion_page: page_id=%s new_parent=%s", page_id, new_parent_id
        )
        # ── Pre-flight parent validation (no API call) ─────────────────
        # Notion's move API only accepts a page or data source parent.
        # LLMs regularly pass the literal string "workspace" (mirroring
        # the ``parent.type == "workspace"`` shape that page *reads*
        # return) — Notion rejects it with an opaque validation error
        # ("parent.page_id should be a valid uuid, instead was
        # `\"workspace\"`"). Fail fast with actionable guidance instead
        # of burning the HTTP call.
        target = (new_parent_id or "").strip()
        if target.lower() in {"workspace", "root"}:
            return (
                "Error: Moving a page to the workspace root is not "
                "supported by the Notion API. new_parent_id must be the "
                "UUID of a target parent page or data source (database). "
                "Pick an existing top-level page (use search_notion to "
                "find one) and move the page under it instead."
            )
        if not is_valid_uuid(target):
            return (
                f"Error: new_parent_id '{target}' is not a valid Notion "
                "UUID. Provide the UUID of the target parent page or "
                "data source (database). Use search_notion or "
                "list_notion_databases to find the correct ID."
            )
        # Same fail-fast as create_notion_page: a placement word like
        # "bottom" must never reach Notion as ``after_block.id``.
        position_payload, position_err = _resolve_position(position)
        if position_err:
            return position_err

        async with _client() as client:
            # ── Auto-detect parent type (mirrors create_notion_page) ──
            # The move API accepts either a page or a data source parent.
            # If the target is a database, resolve its primary data
            # source; otherwise treat it as a page.
            # Both probes are EXPECTED to miss for the common page-parent
            # case: Notion answers 404, or 400 "is a page, not a database"
            # (data sources: an equivalent "is a page" 400) when the id is
            # a regular page. Pass ``expected_statuses={400, 404}`` so the
            # misses stay out of the warning log (and the failed response
            # is returned so we can branch on ``status_code``). Only 200
            # (hit), 404 and the "is a page" 400 (miss) are detection
            # outcomes; any other HTTP status (another 400, 401/403/429/
            # 5xx) or a network error means we CANNOT know the parent's
            # kind, so fail fast with the underlying error instead of
            # guessing "page" and sending a misleading move body to Notion.
            parent: dict | None = None
            proven_page = False
            check, check_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/databases/{target}",
                _PROVIDER,
                "detect move parent type",
                expected_statuses={400, 404},
            )
            if check is None:
                logger.warning(
                    "move_notion_page: parent-type probe failed for %s: %s",
                    target,
                    check_err,
                )
                return check_err or "Error: could not detect the move parent type."
            if check.status_code == 200:
                data_sources = check.json().get("data_sources", [])
                if data_sources:
                    ds_id = data_sources[0]["id"]
                    parent = {"type": "data_source_id", "data_source_id": ds_id}
                    logger.info(
                        "move_notion_page: resolved parent database %s → data_source %s",
                        target,
                        ds_id,
                    )
            elif _is_page_id_400(check):
                # The id exists and is a page: a detection miss, and no
                # later read is needed to prove the parent alive.
                proven_page = True
            elif check.status_code != 404:
                logger.warning(
                    "move_notion_page: parent-type probe failed for %s: %s",
                    target,
                    check_err,
                )
                return check_err or "Error: could not detect the move parent type."

            if parent is None and proven_page:
                parent = {"type": "page_id", "page_id": target}
                logger.debug("move_notion_page: parent is a page")
            if parent is None:
                ds_check, ds_err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/data_sources/{target}",
                    _PROVIDER,
                    "check data source",
                    expected_statuses={400, 404},
                )
                if ds_check is None or (ds_check.status_code not in (200, 404) and not _is_page_id_400(ds_check)):
                    logger.warning(
                        "move_notion_page: data-source probe failed for %s: %s",
                        target,
                        ds_err,
                    )
                    return ds_err or "Error: could not detect the move parent type."
                if ds_check.status_code == 200:
                    parent = {"type": "data_source_id", "data_source_id": target}
                    logger.debug("move_notion_page: parent is a data_source")
                else:
                    parent = {"type": "page_id", "page_id": target}
                    logger.debug("move_notion_page: parent is a page")
                    if not _is_page_id_400(ds_check):
                        await _verify_page_parent(client, target)

            body: dict = {"parent": parent}
            if position_payload:
                body["position"] = position_payload

            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/pages/{page_id}/move",
                _PROVIDER,
                "move page",
                json=body,
            )
            if err:
                # Notion rejects moves of non-page objects (e.g. a block
                # ID passed as page_id) with "Object ... is not a page or
                # database and cannot be moved" — enrich it with recovery
                # guidance so the LLM stops retrying with the same ID.
                if "is not a page or database" in err:
                    err += (
                        "\nThe page_id likely refers to a block (e.g. a "
                        "column or paragraph), not a page. Only pages can "
                        "be moved — pass the ID of the containing page "
                        "instead (use search_notion to find it)."
                    )
                logger.warning("move_notion_page: error for %s: %s", page_id, err)
                return err
            # Both parent-type probes are expected to 404 for a page parent:
            # the move proves the parent, and the page, exist.
            ctx.note_resource_alive(target)
            ctx.note_resource_alive(page_id)
            page = resp.json()
            logger.info("move_notion_page: moved %s to %s", page_id, target)
            return f"Page moved: {page.get('url', '')} (ID: {page['id']}) → parent {target}"

    async def trash_notion_page(page_id: str) -> str:
        """Move a Notion page to the trash (archive it). Can be restored later."""
        logger.info("trash_notion_page: page_id=%s", page_id)
        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "trash page",
                json={"in_trash": True},
            )
            if err:
                logger.warning("trash_notion_page: error for %s: %s", page_id, err)
                return err
            logger.info("trash_notion_page: trashed %s", page_id)
            return f"Page {page_id} moved to trash."

    async def restore_notion_page(page_id: str) -> str:
        """Restore a trashed Notion page (un-archive it) - the reverse of trash_notion_page."""
        logger.info("restore_notion_page: page_id=%s", page_id)
        async with _client() as client:
            _, err = await safe_request(
                client,
                "patch",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "restore page",
                json={"in_trash": False},
            )
            if err:
                logger.warning("restore_notion_page: error for %s: %s", page_id, err)
                return err
            logger.info("restore_notion_page: restored %s", page_id)
            return f"Page {page_id} restored from trash."

    async def get_notion_page_property(page_id: str, property_id: str) -> str:
        """Retrieve a specific property item from a Notion page.

        Use for paginated properties like relations or rollups.
        """
        encoded_prop = quote(unquote(property_id), safe="")
        logger.info(
            "retrieve_notion_page_property: page=%s prop=%s", page_id, property_id
        )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/pages/{page_id}/properties/{encoded_prop}",
                _PROVIDER,
                "retrieve page property",
            )
            if err:
                logger.warning(
                    "retrieve_notion_page_property: error for %s/%s: %s",
                    page_id,
                    property_id,
                    err,
                )
                return err
            data = resp.json()
            logger.info(
                "retrieve_notion_page_property: success for %s/%s", page_id, property_id
            )
            return json.dumps(data, indent=2, default=str)[:4000]

    executor.register("get_notion_page", get_notion_page)
    executor.register("get_notion_page_properties", get_notion_page_properties)
    executor.register("create_notion_page", create_notion_page)
    executor.register("update_notion_page", update_notion_page)
    executor.register("move_notion_page", move_notion_page)
    executor.register("trash_notion_page", trash_notion_page)
    executor.register("restore_notion_page", restore_notion_page)
    executor.register("get_notion_page_property", get_notion_page_property)

    return [
        # ── Pages ──────────────────────────────────────────────────────
        ToolDefinition(
            name="get_notion_page",
            priority=0,
            description=(
                "Read the content of a Notion page (including nested blocks), "
                f"{_READ_PAGE_MAX_CHARS:,} characters per call by default: a "
                "longer page ends with the offset to continue from - pass it as "
                f"offset, or raise max_chars (up to {_READ_PAGE_WINDOW_MAX_CHARS:,}) "
                "to read a long page in one call. For pages inside databases, "
                "also includes all database properties (metadata columns) and "
                "lists any unset properties. Relation properties are resolved to "
                "page titles. Only use this when the search preview is "
                "insufficient - prefer search_notion for quick lookups."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Notion page ID"},
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Character offset into the rendered page to start "
                            "from (default 0). Use the value a previous call gave "
                            "to continue."
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _READ_PAGE_WINDOW_MAX_CHARS,
                        "description": (
                            f"Characters to return (default {_READ_PAGE_MAX_CHARS:,}, "
                            f"max {_READ_PAGE_WINDOW_MAX_CHARS:,})."
                        ),
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="get_notion_page_properties",
            description=(
                "Get all database properties (metadata) for a Notion page "
                "without reading the page content. Returns a detailed per-property "
                "breakdown including: property name, type, and current value. "
                "Relation properties are resolved to page titles instead of raw IDs. "
                "Rollup values are expanded. Empty/unset properties are explicitly "
                "listed. Use this to inspect or verify database fields like "
                "Product Owner, Status, Priority, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID (must be a page inside a database)",
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="create_notion_page",
            priority=0,
            description=(
                "Create a new page in Notion under a parent page, database, or "
                "data source. The content field accepts rich Markdown that is "
                "converted to native Notion blocks: # to #### headings "
                "(##### and deeper map to heading 4), - / * / + bullets, "
                "1. / 1) numbered lists (indent a line to nest it one level), "
                "[] / [x] to-dos, > quotes, --- dividers, | a | b | tables, and "
                "``` fenced code blocks. Inline **bold**, *italic*, `code`, "
                "~~strikethrough~~ and [label](https://url) render as real "
                "formatting - write Markdown directly, never escape the markers. "
                f"{MARKDOWN_LAYOUT_SYNTAX} "
                "Optionally set an icon emoji and cover image URL. To create "
                "from a template, set template_id (use list_notion_templates to "
                "discover IDs); content is ignored when a template is used."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "Parent page ID, database ID, or data_source ID (auto-detected)",
                    },
                    "title": {"type": "string", "description": "Page title"},
                    "content": {
                        "type": "string",
                        "description": (
                            "Page content as Markdown. Block syntax: # / ## / ### "
                            "headings, - / * / + bullets (indent to nest), 1. / 1) "
                            "numbered lists, [] / [x] to-dos, > quotes, --- dividers, "
                            "| a | b | tables, ``` fenced code blocks, "
                            "> [!icon=... color=...] callouts, {color=...} suffix, "
                            "::: columns / ::: column / ::: layout. Inline: "
                            "**bold**, *italic*, `code`, ~~strikethrough~~, "
                            "[label](https://url). Ignored when template_id is set."
                        ),
                        "default": "",
                    },
                    "position": {
                        "type": "string",
                        "description": (
                            "Where to insert: 'start' for top of parent, the UUID "
                            "of an existing sibling block to insert after, or omit "
                            "(or 'end'/'bottom') for bottom (default). Never pass "
                            "placement words as a block ID."
                        ),
                        "default": "",
                    },
                    "icon_emoji": {
                        "type": "string",
                        "description": "Emoji for the page icon (e.g. '📋')",
                        "default": "",
                    },
                    "cover_url": {
                        "type": "string",
                        "description": "External URL for the page cover image",
                        "default": "",
                    },
                    "template_id": {
                        "type": "string",
                        "description": (
                            "Template ID to pre-populate the page from (use "
                            "list_notion_templates to find IDs). Set to 'default' "
                            "for the data source's default template. When set, "
                            "content is ignored — Notion populates the page "
                            "asynchronously from the template."
                        ),
                        "default": "",
                    },
                },
                "required": ["parent_id", "title"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="update_notion_page",
            description=(
                "Update a Notion page: properties, icon, cover, lock status, "
                "and/or apply a template. "
                'Properties example: {"Status": {"select": {"name": "Done"}}}. '
                "Set icon_emoji to an emoji or 'null' to remove. "
                "Set cover_url to a URL or 'null' to remove. "
                "Set is_locked to 'true'/'false' to lock/unlock from UI editing. "
                "Set template_id to apply a template to the page (use "
                "list_notion_templates to find IDs, or 'default' for the "
                "data source's default template)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Notion page ID"},
                    "properties_json": {
                        "type": "string",
                        "description": "Properties to update as JSON string (optional)",
                        "default": "",
                    },
                    "icon_emoji": {
                        "type": "string",
                        "description": "Emoji for icon, or 'null' to remove (optional)",
                        "default": "",
                    },
                    "cover_url": {
                        "type": "string",
                        "description": "External URL for cover image, or 'null' to remove (optional)",
                        "default": "",
                    },
                    "is_locked": {
                        "type": "string",
                        "description": "'true' to lock page from UI editing, 'false' to unlock (optional)",
                        "default": "",
                    },
                    "template_id": {
                        "type": "string",
                        "description": (
                            "Apply a template to the page. Use a template ID from "
                            "list_notion_templates, or 'default' for the data source's "
                            "default template. Template content is appended to existing "
                            "content unless erase_content is 'true'."
                        ),
                        "default": "",
                    },
                    "erase_content": {
                        "type": "string",
                        "description": (
                            "'true' to replace existing page content with template "
                            "content (destructive, cannot be undone via API). Only "
                            "used with template_id."
                        ),
                        "default": "",
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="move_notion_page",
            description=(
                "Move a Notion page under a different parent page or into a "
                "database (data source). Optionally specify position (top or "
                "after a specific block). Moving to the workspace root is NOT "
                "supported — the new parent must be an existing page or "
                "database UUID."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID to move (must be a page, not a block)",
                    },
                    "new_parent_id": {
                        "type": "string",
                        "description": (
                            "UUID of the new parent page, database, or data source "
                            "(databases are auto-resolved). 'workspace' is NOT valid — "
                            "moving to the workspace root is not supported"
                        ),
                    },
                    "position": {
                        "type": "string",
                        "description": (
                            "Where to place in new parent: 'start' for top, the "
                            "UUID of a sibling block to insert after, or omit "
                            "(or 'end'/'bottom') for bottom (default)"
                        ),
                        "default": "",
                    },
                },
                "required": ["page_id", "new_parent_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="trash_notion_page",
            description="Move a Notion page to the trash (archive it). Reversible with restore_notion_page.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID to trash",
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="restore_notion_page",
            description="Restore a trashed Notion page (un-archive it). The reverse of trash_notion_page.",
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID to restore from trash",
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=2,
        ),
        ToolDefinition(
            name="get_notion_page_property",
            description=(
                "Retrieve a specific property from a Notion page. Useful for paginated "
                "properties like relations, rollups, or people. Use get_notion_database "
                "or get_notion_page_properties first to discover valid property IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Notion page ID"},
                    "property_id": {
                        "type": "string",
                        "description": (
                            "Property ID from the Notion API (e.g. 'title', 'Abc1', "
                            "or 'notion%3A%2F%2Ftasks%2Ftask_to_project_relation'). "
                            "Obtain IDs from get_notion_database or get_notion_page_properties. "
                            "Do NOT invent property IDs — use the exact 'id' field from the API response."
                        ),
                    },
                },
                "required": ["page_id", "property_id"],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
