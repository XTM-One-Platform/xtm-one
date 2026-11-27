"""Notion formatting helpers.

Markdown <-> Notion block / rich-text conversion, property summarisation, and
database-schema rendering. Moved verbatim from the bottom helpers region of the
original ``notion.py``.
"""

from __future__ import annotations

import re
from typing import Any

from ._common import (
    _NOTION_BLOCK_TYPES,
    _NOTION_COLOR_NAMES,
    _NOTION_COLORS,
    _NOTION_RICH_TEXT_LIMIT,
    _extract_title,
    _person_label,
    _rich_text_to_plain,
    logger,
    unnamed_people_not_collected,
)


def _format_database_schema(db: dict, *, is_data_source: bool = False) -> str:
    """Format a database/data_source schema response into a readable string."""
    title = _extract_title(db)
    obj_type = "Data Source" if is_data_source else "Database"
    props = db.get("properties", {})

    lines = [
        f"{obj_type}: {title}",
        f"ID: {db['id']}",
        f"URL: {db.get('url', '')}",
    ]

    # Include parent info if available
    parent = db.get("parent", {})
    parent_type = parent.get("type", "")
    if parent_type == "database_id":
        lines.append(f"Parent database: {parent.get('database_id', '')}")
    elif parent_type == "page_id":
        lines.append(f"Parent page: {parent.get('page_id', '')}")
    # Include database_parent (grandparent) for data_sources
    db_parent = db.get("database_parent", {})
    if db_parent:
        dp_type = db_parent.get("type", "")
        if dp_type == "page_id":
            lines.append(f"Database parent page: {db_parent.get('page_id', '')}")

    lines.extend(["", "Properties (columns):"])
    for name, prop in props.items():
        prop_type = prop.get("type", "unknown")
        detail = ""
        if prop_type == "select":
            options = [
                o.get("name", "") for o in prop.get("select", {}).get("options", [])
            ]
            detail = f" (options: {', '.join(options)})" if options else ""
        elif prop_type == "multi_select":
            options = [
                o.get("name", "")
                for o in prop.get("multi_select", {}).get("options", [])
            ]
            detail = f" (options: {', '.join(options)})" if options else ""
        elif prop_type == "status":
            options = [
                o.get("name", "") for o in prop.get("status", {}).get("options", [])
            ]
            detail = f" (options: {', '.join(options)})" if options else ""
        elif prop_type == "relation":
            rel = prop.get("relation", {})
            ds_id = rel.get("data_source_id", "")
            db_id = rel.get("database_id", "?")
            detail = (
                f" (→ data_source {ds_id}, database {db_id})"
                if ds_id
                else f" (→ database {db_id})"
            )
        lines.append(f"  • {name}: {prop_type}{detail} (id: {prop.get('id', '')})")
    return "\n".join(lines)


def _sanitize_rich_text(rt: list) -> list:
    """Fix LLM-generated rich_text: move misplaced annotations and enforce Notion's 2000-char limit.

    The Notion API expects annotations as a sibling of text, not nested inside:
      Correct:   {"text": {"content": "Hi"}, "annotations": {"bold": true}}
      Incorrect: {"text": {"content": "Hi", "annotations": {"bold": true}}}

    Also truncates text.content to _NOTION_RICH_TEXT_LIMIT (2000) chars to prevent
    HTTP 400 errors from the Notion API.
    """
    for item in rt:
        # The array is LLM-generated, so an item may be a bare string / null
        # instead of a rich_text object. Skip non-dicts (best-effort) rather
        # than crash the whole tool call on item.get(...) - the Notion API
        # returns its own actionable error for a genuinely malformed payload.
        if not isinstance(item, dict):
            continue
        text_obj = item.get("text")
        if isinstance(text_obj, dict):
            if "annotations" in text_obj:
                annotations = text_obj.pop("annotations")
                existing = item.get("annotations", {})
                item["annotations"] = {**existing, **annotations}
            content = text_obj.get("content")
            if isinstance(content, str) and len(content) > _NOTION_RICH_TEXT_LIMIT:
                logger.warning(
                    "Notion rich_text content truncated from %d to %d chars",
                    len(content),
                    _NOTION_RICH_TEXT_LIMIT,
                )
                text_obj["content"] = content[: _NOTION_RICH_TEXT_LIMIT - 1] + "…"
    return rt


def _truncate_properties_rich_text(properties: dict) -> None:
    """Truncate text content in page properties to Notion's 2000-char limit.

    Walks a Notion ``properties`` dict and applies :func:`_sanitize_rich_text`
    to every text-bearing list it finds.  Both Notion property types covered:

    * ``rich_text`` properties — used by ``update_notion_page`` and
      ``create_notion_page(properties_json=...)`` for free-text fields like
      "Handoff Summary".
    * ``title`` properties — the per-row title in a database (built directly
      from the ``title`` argument of ``create_notion_page`` at line 637/653,
      and frequently mirrored by callers passing
      ``properties_json={"Name": {"title": [...]}}``).  ``title`` items are
      subject to the same 2 000-character limit per text item, so without
      this branch a >2 000-character title would still produce an HTTP 400
      from the Notion API — exactly the symptom this PR sets out to fix.

    Modification is in place; the function returns ``None``.
    """
    for prop_value in properties.values():
        if not isinstance(prop_value, dict):
            continue
        for key in ("rich_text", "title"):
            items = prop_value.get(key)
            if isinstance(items, list):
                _sanitize_rich_text(items)


def _normalize_table_row_cells(cells: list) -> list:
    """Normalize ``table_row.cells`` to ``[[rich_text_obj, ...], ...]``.

    Notion requires each cell to be a list of rich-text objects.  The LLM
    frequently passes plain strings (``"Alice"``) instead of the required
    rich-text object form (``[{"type": "text", "text": {"content": "Alice"}}]``),
    which causes Notion's API to reject every update with HTTP 400.

    Handles all LLM-generated cell shapes:

    * ``"string"``               – bare string → wrapped as a rich-text list
    * ``""``                     – empty string → an empty cell (``[]``)
    * ``42`` / ``3.14``          – numeric scalar → stringified and wrapped
    * ``["string", ...]``        – list containing strings → each string wrapped
    * ``[{rich_text_obj}, ...]`` – already correct; ``_sanitize_rich_text`` run
                                   in place (fixes misplaced annotations, trims)
    * ``{rich_text_obj}``        – bare dict → wrapped in a list and sanitized

    Returns a new ``list[list[dict]]``.
    """
    normalized: list[list[dict]] = []
    for cell in cells:
        if isinstance(cell, str) and not cell:
            # An empty cell: ``""`` is how a model writes one, Notion wants ``[]``.
            cell_rt: list[dict] = []
        elif isinstance(cell, str):
            # Most common bug: the LLM passed a bare string instead of a
            # rich-text list.  Wrap it in the required [[{...}]] structure.
            cell_rt = [{"type": "text", "text": {"content": cell}}]
        elif isinstance(cell, (int, float)):
            # Numeric scalar (the LLM sends numbers for numeric columns) —
            # stringify rather than silently dropping the value.
            cell_rt = [{"type": "text", "text": {"content": str(cell)}}]
        elif isinstance(cell, list):
            cell_rt = []
            for item in cell:
                if isinstance(item, str):
                    cell_rt.append({"type": "text", "text": {"content": item}})
                elif isinstance(item, (int, float)):
                    cell_rt.append({"type": "text", "text": {"content": str(item)}})
                elif isinstance(item, dict):
                    cell_rt.append(item)
                # else: skip other non-dict items (best-effort)
        elif isinstance(cell, dict):
            # A bare rich-text dict used instead of a list — wrap it.
            cell_rt = [cell]
        else:
            # Unrecognised shape; emit an empty cell rather than crash.
            cell_rt = []
        # Sanitize every branch uniformly so wrapped scalars get the same
        # misplaced-annotation fix and 2000-char truncation as object cells.
        normalized.append(_sanitize_rich_text(cell_rt))
    return normalized


# Bounds the ``children`` recursion in ``_sanitize_blocks``. Notion itself
# only accepts two nesting levels per request, so 8 is comfortably above any
# legitimate payload while still guarding against a pathological cyclic dict.
_MAX_SANITIZE_DEPTH = 8


def _sanitize_blocks(blocks: list[dict], _depth: int = 0) -> list[dict]:
    """Sanitize LLM-generated Notion block JSON before sending to the API.

    Handles both block shapes the tools produce:

    * **append shape** — ``{"type": "paragraph", "paragraph": {...}}`` (carries
      an explicit ``type`` discriminator), as sent by
      ``append_notion_blocks_json``.
    * **update shape** — ``{"paragraph": {...}}`` (no ``type`` key; the block
      type IS the top-level key), as sent by ``update_notion_block``.

    Resolving the body only via ``block["type"]`` skipped the update shape
    entirely, so the rich-text hardening (annotations moved to a sibling of
    ``text``) and the 2000-char truncation that prevents HTTP 400s never ran
    for ``update_notion_block`` payloads.

    Also normalises ``table_row.cells`` from bare strings / mixed shapes to the
    ``[[rich_text_obj, ...], ...]`` structure Notion's API requires,
    and recurses into nested ``children`` (bounded by ``_MAX_SANITIZE_DEPTH``)
    so table rows nested inside a ``table`` block — the shape
    ``append_notion_blocks_json`` sends — get the same treatment.
    """
    for block in blocks:
        # The array is LLM-generated, so an entry may be a non-dict (string /
        # null). Skip it (best-effort) rather than crash on block.get(...) -
        # the Notion API returns its own actionable error for a malformed block.
        if not isinstance(block, dict):
            continue
        # The body lives under a key named after the block type. Cover the
        # explicit ``type`` discriminator (append shape) AND any top-level key
        # that names a Notion block type (update shape, which omits ``type``).
        body_keys = {block.get("type", "")} | {
            k for k in block if k in _NOTION_BLOCK_TYPES
        }
        for body_key in body_keys:
            body = block.get(body_key) if body_key else None
            if isinstance(body, dict):
                rt = body.get("rich_text")
                if isinstance(rt, list):
                    body["rich_text"] = _sanitize_rich_text(rt)
                # table_row.cells is [[rich_text_obj, ...], ...] but the LLM
                # often sends bare strings — normalise in place.
                if body_key == "table_row":
                    cells = body.get("cells")
                    if isinstance(cells, list):
                        body["cells"] = _normalize_table_row_cells(cells)
                # Nested blocks (e.g. table_row children of a table block,
                # list-item children) need the same hardening.
                children = body.get("children")
                if isinstance(children, list) and _depth < _MAX_SANITIZE_DEPTH:
                    _sanitize_blocks(children, _depth + 1)
    return blocks


# The alignment walk is a generic key search, so its bound counts dict / list
# levels rather than block levels: one block level spans about three
# containers (block -> body -> children list -> child block), so this is
# ``_MAX_SANITIZE_DEPTH`` block levels expressed in container levels, with
# room for a file-source / caption hop. A safety net only - Notion itself
# accepts two levels of nesting per request.
_MAX_ALIGNMENT_SCAN_DEPTH = _MAX_SANITIZE_DEPTH * 4


def _join_alignment_path(prefix: str, found: str) -> str:
    """``prefix`` + ``found`` with a dot, except before a list index (``a[0].b``)."""
    return f"{prefix}{found}" if found.startswith("[") else f"{prefix}.{found}"


def _find_block_alignment(block: Any, _depth: int = 0) -> str | None:
    """Return the path of an ``alignment`` key anywhere inside one block payload, or ``None``.

    Notion exposes no alignment for any block through its API (see
    ``_common._NOTION_ALIGNMENT_UI_ONLY``) and NO request field of any block
    type is named ``alignment`` - so the key is a refusal wherever a model
    puts it within the scan bound: in the block body (the update shape
    ``{"image": {"alignment": "left"}}`` or the append shape ``{"type":
    "image", "image": {...}}``), at the top level, inside the file source
    (``image.external.alignment``), on a caption item, or in nested
    ``children``. The walk is therefore a generic key search over dicts and
    lists in payload order, and the returned path names the exact key the
    agent has to drop (``image.alignment``,
    ``column_list.children[0].column.children[1].image.alignment``).

    The walk stops at ``_MAX_ALIGNMENT_SCAN_DEPTH`` container levels so an
    absurd payload cannot recurse forever; a key buried deeper than that is
    NOT found and the payload goes to Notion. That bound sits far past
    Notion's own two-levels-of-nesting limit per request, so such a payload
    is malformed on its own terms and Notion refuses it regardless - the
    guarantee is "no alignment key reaches Notion in a payload it could
    otherwise accept", not "every conceivable payload is scanned in full".
    """
    if _depth > _MAX_ALIGNMENT_SCAN_DEPTH:
        return None
    if isinstance(block, dict):
        if "alignment" in block:
            return "alignment"
        for key, value in block.items():
            found = _find_block_alignment(value, _depth + 1)
            if found is not None:
                return _join_alignment_path(str(key), found)
        return None
    if isinstance(block, list):
        for idx, item in enumerate(block):
            found = _find_block_alignment(item, _depth + 1)
            if found is not None:
                return _join_alignment_path(f"[{idx}]", found)
        return None
    return None


class MarkdownBlockError(ValueError):
    """A layout directive in the Markdown the converter cannot honour.

    Raised while the Markdown is converted, before any request: the write
    tools refuse the whole call and nothing is written, since a layout half
    applied is worse than none.
    """


def _notion_color(value: str) -> str:
    color = value.strip().lower()
    if color not in _NOTION_COLORS:
        names = ", ".join(_NOTION_COLOR_NAMES)
        raise MarkdownBlockError(
            f"{value!r} is not a Notion colour; use one of {names}, or one of "
            "them but default followed by _background (e.g. blue_background)"
        )
    return color


def _notion_icon(value: str) -> dict:
    icon = value.strip()
    if not icon:
        raise MarkdownBlockError("`icon=` needs an emoji or an https:// image URL")
    if icon.lower().startswith(("https://", "http://")):
        return {"type": "external", "external": {"url": icon}}
    return {"type": "emoji", "emoji": icon}


# A trailing ``{color=...}`` (and, on a callout, ``icon=...``) on a text block,
# the form ``_common._block_to_text`` writes a block's colour in.
_BLOCK_ATTRS_RE = re.compile(
    r"\s*\{\s*((?:(?:color|icon)\s*=\s*[^\s{}]+\s*)+)\}\s*\Z", re.IGNORECASE
)
_BLOCK_ATTR_PAIR_RE = re.compile(r"(color|icon)\s*=\s*([^\s{}]+)", re.IGNORECASE)


def _split_block_attrs(text: str) -> tuple[str, dict[str, str]]:
    """*text* without a trailing ``{color=... icon=...}``, and the attributes it set."""
    match = _BLOCK_ATTRS_RE.search(text)
    if not match:
        return text, {}
    attrs: dict[str, str] = {}
    for key, value in _BLOCK_ATTR_PAIR_RE.findall(match.group(1)):
        key = key.lower()
        if key in attrs:
            raise MarkdownBlockError(f"`{key}` is set twice in {match.group(0).strip()}")
        attrs[key] = value
    return text[: match.start()], attrs


def _block_attrs_body(attrs: dict[str, str], *, allow_icon: bool) -> dict:
    """The block body fields *attrs* set: ``color``, and ``icon`` on a callout."""
    body: dict = {}
    if "color" in attrs:
        body["color"] = _notion_color(attrs["color"])
    if "icon" in attrs:
        if not allow_icon:
            raise MarkdownBlockError(
                "`icon` is set on a callout only: write `> [!icon=EMOJI] text`"
            )
        body["icon"] = _notion_icon(attrs["icon"])
    return body


def _text_block_body(text: str) -> dict:
    """The body of a text block: its inline Markdown, and the colour a trailing ``{color=...}`` sets."""
    content, attrs = _split_block_attrs(text)
    return {"rich_text": _rich_text(content), **_block_attrs_body(attrs, allow_icon=False)}


# ``<!-- table: row_header -->`` on the line before a table sets its header
# flags; each flag names the header it sets and whether it is on.
_TABLE_HINT_RE = re.compile(r"<!--\s*table\s*:\s*(.*?)\s*-->", re.IGNORECASE)
_TABLE_HINT_FLAGS: dict[str, tuple[str, bool]] = {
    "row_header": ("has_row_header", True),
    "no_row_header": ("has_row_header", False),
    "column_header": ("has_column_header", True),
    "no_column_header": ("has_column_header", False),
}


def _parse_table_hint(spec: str) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for token in re.split(r"[\s,]+", spec.strip()):
        if not token:
            continue
        flag = _TABLE_HINT_FLAGS.get(token.lower())
        if flag is None:
            raise MarkdownBlockError(
                f"`{token}` is not a table flag in `<!-- table: {spec} -->`; use "
                + ", ".join(_TABLE_HINT_FLAGS)
            )
        name, value = flag
        if flags.get(name, value) != value:
            raise MarkdownBlockError(f"`<!-- table: {spec} -->` turns a header both on and off")
        flags[name] = value
    if not flags:
        raise MarkdownBlockError(
            "`<!-- table: -->` names no flag; use " + ", ".join(_TABLE_HINT_FLAGS)
        )
    return flags


def _is_table_row(line: str) -> bool:
    """Return True if *line* looks like a markdown table row (``| … | … |``)."""
    stripped = line.strip()
    return (
        stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3
    )


def _is_separator_row(line: str) -> bool:
    """Return True if *line* is a markdown table separator (``|---|---|``).

    Each column cell must consist only of ``-`` / ``:`` (alignment markers) with
    at least one dash. The previous single ``strip('|')`` left inner pipes in
    multi-column separators, so ``| --- | --- |`` was misread as a data row and
    leaked into the Notion table as a row of literal ``---`` cells.
    """
    stripped = line.strip().strip("|")
    if not stripped:
        return False
    for cell in stripped.split("|"):
        c = cell.strip()
        if not c or set(c) - {"-", ":"} or "-" not in c:
            return False
    return True


def _parse_table_row(line: str) -> list[str]:
    """Split a markdown table row into cell strings."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _build_table_block(
    rows: list[list[str]],
    *,
    has_column_header: bool | None = None,
    has_row_header: bool | None = None,
) -> dict:
    """Build a Notion ``table`` block from parsed rows.

    The first row is the column header, unless its cells are all empty: that
    is a key-value table whose first COLUMN is the header (``| | |`` above
    ``| **Date** | Friday |``), so the blank row is dropped and the table gets
    a row header instead. *has_column_header* / *has_row_header*
    (a ``<!-- table: ... -->`` hint) override either default. Column count
    is normalised to the widest row so Notion doesn't reject the request.
    """
    if not rows:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": []},
        }

    col_count = max(len(r) for r in rows)
    key_value = len(rows) > 1 and all(not cell.strip() for cell in rows[0])
    if key_value:
        rows = rows[1:]
    if has_column_header is None:
        has_column_header = not key_value
    if has_row_header is None:
        has_row_header = key_value

    table_rows: list[dict] = []
    for row in rows:
        padded = row + [""] * (col_count - len(row))
        # Inline-parse each cell so **bold** / `code` / [links](url) render as
        # native Notion formatting instead of literal characters.
        cells = [_parse_inline_markdown(cell) for cell in padded]
        table_rows.append({"type": "table_row", "table_row": {"cells": cells}})

    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": col_count,
            "has_column_header": has_column_header,
            "has_row_header": has_row_header,
            "children": table_rows,
        },
    }


# ── Inline Markdown -> Notion rich_text ─────────────────────────────────────
# Notion stores character-level formatting as ``annotations`` (a sibling of
# ``text``) and links as ``text.link.url``. The LLM authors content in
# Markdown, so without this parser markers like **bold**, *italic*, `code`,
# ~~strike~~ and [label](url) end up as literal characters on the page.

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")
_MD_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_ALT_RE = re.compile(r"(?<![A-Za-z0-9])__(.+?)__(?![A-Za-z0-9])", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"\*(\S(?:[^*]*?\S)?)\*", re.DOTALL)
_MD_ITALIC_ALT_RE = re.compile(
    r"(?<![A-Za-z0-9_])_(\S(?:[^_]*?\S)?)_(?![A-Za-z0-9_])", re.DOTALL
)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_CODE_RE = re.compile(r"`([^`]+)`")
# ``@[Name](user:<id>)``: a user mention, the form the read tools show one in
# (``_common._user_mention_text``).
_MD_USER_MENTION_RE = re.compile(r"@\[([^\]]+)\]\(user:([0-9A-Za-z-]+)\)")

# (priority, regex, kind). On a positional tie the lower priority wins, so a
# longer marker (``***``) is preferred over the shorter ``**`` / ``*`` that
# would also match at the same index.
_INLINE_RULES: tuple[tuple[int, re.Pattern, str], ...] = (
    (0, _MD_CODE_RE, "code"),
    (0, _MD_USER_MENTION_RE, "user_mention"),
    (0, _MD_LINK_RE, "link"),
    (1, _MD_BOLD_ITALIC_RE, "bold_italic"),
    (2, _MD_BOLD_RE, "bold"),
    (2, _MD_BOLD_ALT_RE, "bold"),
    (3, _MD_STRIKE_RE, "strike"),
    (4, _MD_ITALIC_RE, "italic"),
    (4, _MD_ITALIC_ALT_RE, "italic"),
)

_INLINE_ANNOTATIONS: dict[str, tuple[str, ...]] = {
    "bold_italic": ("bold", "italic"),
    "bold": ("bold",),
    "italic": ("italic",),
    "strike": ("strikethrough",),
}

# Bounds marker NESTING depth (e.g. a bold link whose label has code). Sequential
# markers on one line are handled iteratively, so this never limits line length.
_MAX_INLINE_DEPTH = 8


def _append_inline_text(
    out: list[dict],
    content: str,
    annotations: dict | None,
    link: str | None,
) -> None:
    """Append rich_text item(s) for *content*, splitting on the 2000-char limit
    and attaching annotations/link when present."""
    if not content:
        return
    for start in range(0, len(content), _NOTION_RICH_TEXT_LIMIT):
        chunk = content[start : start + _NOTION_RICH_TEXT_LIMIT]
        text_obj: dict[str, Any] = {"content": chunk}
        if link:
            text_obj["link"] = {"url": link}
        item: dict[str, Any] = {"type": "text", "text": text_obj}
        if annotations:
            item["annotations"] = dict(annotations)
        out.append(item)


def _parse_inline_markdown(
    text: str,
    annotations: dict | None = None,
    link: str | None = None,
    _depth: int = 0,
) -> list[dict]:
    """Convert inline Markdown in *text* to a Notion rich_text array.

    Handles **bold**, *italic* / _italic_, ``code``, ~~strikethrough~~,
    ***bold italic***, [label](url) and the user mention @[Name](user:<id>).
    Markers nest (e.g. a bold link), runs longer than 2000 chars are split,
    and an empty string yields ``[]``.
    """
    annotations = annotations or {}
    out: list[dict] = []
    if not text:
        return out
    if _depth >= _MAX_INLINE_DEPTH:
        _append_inline_text(out, text, annotations, link)
        return out

    pos = 0
    length = len(text)
    while pos < length:
        rest = text[pos:]
        best: tuple[int, int, str, re.Match] | None = None
        for priority, regex, kind in _INLINE_RULES:
            m = regex.search(rest)
            if m and (best is None or (m.start(), priority) < (best[0], best[1])):
                best = (m.start(), priority, kind, m)
        if best is None:
            _append_inline_text(out, rest, annotations, link)
            break
        start, _priority, kind, match = best
        if start > 0:
            _append_inline_text(out, rest[:start], annotations, link)
        inner = match.group(1)
        if kind == "code":
            # Code spans are terminal: never reinterpret their contents.
            _append_inline_text(out, inner, {**annotations, "code": True}, link)
        elif kind == "user_mention":
            mention: dict[str, Any] = {
                "type": "mention",
                "mention": {"type": "user", "user": {"id": match.group(2)}},
            }
            if annotations:
                mention["annotations"] = dict(annotations)
            out.append(mention)
        elif kind == "link":
            out.extend(
                _parse_inline_markdown(inner, annotations, match.group(2), _depth + 1)
            )
        else:
            new_ann = dict(annotations)
            for flag in _INLINE_ANNOTATIONS[kind]:
                new_ann[flag] = True
            out.extend(_parse_inline_markdown(inner, new_ann, link, _depth + 1))
        pos += match.end()
    return out


def _rich_text(text: str) -> list[dict]:
    """Inline-parse *text*, always returning at least one rich_text item.

    Headings and list items are happier with an explicit empty run than an
    empty list, so callers that may pass blank text use this wrapper.
    """
    return _parse_inline_markdown(text) or [{"type": "text", "text": {"content": ""}}]


# ── Fenced code blocks ──────────────────────────────────────────────────────
# Notion code blocks only accept a language from a fixed enum; anything else is
# rejected with HTTP 400. Map common aliases and fall back to "plain text".
_NOTION_CODE_LANGUAGES = frozenset(
    {
        "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript",
        "c++", "c#", "css", "dart", "diff", "docker", "elixir", "elm",
        "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go", "graphql",
        "groovy", "haskell", "html", "java", "javascript", "json", "julia",
        "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile",
        "markdown", "markup", "matlab", "mermaid", "nix", "objective-c",
        "ocaml", "pascal", "perl", "php", "plain text", "powershell", "prolog",
        "protobuf", "python", "r", "reason", "ruby", "rust", "sass", "scala",
        "scheme", "scss", "shell", "sql", "swift", "typescript", "vb.net",
        "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
    }
)

_NOTION_CODE_LANG_ALIASES = {
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "py": "python",
    "py3": "python",
    "python3": "python",
    "sh": "shell",
    "zsh": "shell",
    "console": "shell",
    "shell-session": "shell",
    "yml": "yaml",
    "jsonc": "json",
    "json5": "json",
    "htm": "html",
    "cpp": "c++",
    "cs": "c#",
    "csharp": "c#",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "kt": "kotlin",
    "md": "markdown",
    "text": "plain text",
    "txt": "plain text",
    "plaintext": "plain text",
    "": "plain text",
}


def _normalize_notion_language(language: str) -> str:
    lang = (language or "").strip().lower()
    if lang in _NOTION_CODE_LANG_ALIASES:
        return _NOTION_CODE_LANG_ALIASES[lang]
    if lang in _NOTION_CODE_LANGUAGES:
        return lang
    return "plain text"


def _code_block(code: str, language: str = "") -> dict:
    rich: list[dict] = []
    _append_inline_text(rich, code, None, None)
    if not rich:
        rich = [{"type": "text", "text": {"content": ""}}]
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": rich,
            "language": _normalize_notion_language(language),
        },
    }


# ── Lists (with one nesting level) ──────────────────────────────────────────
_NUMBERED_LIST_RE = re.compile(r"\d{1,9}[.)]\s+(.*)", re.DOTALL)


def _parse_list_marker(stripped: str) -> tuple[str, str, bool] | None:
    """Detect a list item. Returns ``(block_type, text, checked)`` or None."""
    if stripped.startswith(("[x] ", "[X] ")):
        return "to_do", stripped[4:], True
    if stripped.startswith("[ ] "):
        return "to_do", stripped[4:], False
    if stripped.startswith("[] "):
        return "to_do", stripped[3:], False
    if stripped.startswith(("- ", "* ", "+ ")):
        return "bulleted_list_item", stripped[2:], False
    if stripped[:1].isdigit():
        m = _NUMBERED_LIST_RE.fullmatch(stripped)
        if m:
            return "numbered_list_item", m.group(1), False
    return None


def _list_item_block(block_type: str, text: str, checked: bool = False) -> dict:
    body: dict[str, Any] = _text_block_body(text)
    if block_type == "to_do":
        body["checked"] = checked
    return {"object": "block", "type": block_type, block_type: body}


def _build_nested_list(items: list[tuple[int, str, str, bool]]) -> list[dict]:
    """Build list blocks (with one nesting level) from parsed items.

    Each item is ``(indent, block_type, text, checked)``. Items more indented
    than the run's base indent become children of the most recent top-level
    item — capped at Notion's two-level single-request limit.
    """
    result: list[dict] = []
    base_indent = items[0][0] if items else 0
    current_top: dict | None = None
    current_top_type: str | None = None
    for indent, block_type, text, checked in items:
        block = _list_item_block(block_type, text, checked)
        if indent > base_indent and current_top is not None and current_top_type:
            current_top[current_top_type].setdefault("children", []).append(block)
        else:
            result.append(block)
            current_top = block
            current_top_type = block_type
    return result


def _heading_from_line(content: str) -> tuple[str, str] | None:
    """Return ``(heading_type, text)`` for a ``#``..``######`` line, else None.

    Notion has heading_1..heading_4, so ``#####`` and ``######`` collapse to
    heading_4. A space after the hashes is required (so ``#tag`` stays a
    paragraph).
    """
    if not content.startswith("#"):
        return None
    hashes = len(content) - len(content.lstrip("#"))
    if hashes == 0 or hashes > 6:
        return None
    rest = content[hashes:]
    if not rest.startswith(" "):
        return None
    return f"heading_{min(hashes, 4)}", rest[1:].strip()


# The layout syntax of ``_text_to_blocks``, as the Markdown write tools
# describe it.
MARKDOWN_LAYOUT_SYNTAX = (
    "Layouts: `> [!icon=🎯 color=blue_background] ## Title` makes a callout "
    "(text after the marker is its text, or a heading inside it; the `>` lines "
    "right after it are its content; [!NOTE] / [!TIP] / [!WARNING] work too); "
    "a trailing `{color=blue}` colours a heading, paragraph, list item or quote; "
    "`::: columns`, then `::: column` before each column and `:::` to close, "
    "lays content out side by side. A table whose first row is blank (`| | |`) "
    "gets its first column as the header; `<!-- table: row_header -->` (or "
    "column_header / no_row_header / no_column_header) on the line before a "
    "table sets its header flags."
)


def _text_to_blocks(text: str) -> list[dict]:
    """Convert markdown-like text to Notion blocks.

    Block-level syntax:
      # / ## / ### / #### Heading (#####-###### collapse to heading_4)
      - / * / + Bullet list item    1. / 1) Numbered list item
      [] / [ ] To-do                [x] Checked to-do
      > Quote                       --- / *** / ___ Divider
      | a | b |                     Consecutive pipe rows -> a Notion table
      ```lang ... ```               Fenced code block
    Indented list items nest one level under the preceding item.

    Layout syntax:
      ``> [!icon=🎯 color=blue_background] ## Title`` - a callout; text after
        the marker is its text, or a heading child when it is a heading; the
        ``>`` lines right after it are its children. ``[!NOTE]`` / ``[!TIP]``
        / ``[!IMPORTANT]`` / ``[!WARNING]`` / ``[!CAUTION]`` give a default
        icon and colour.
      ``## Title {color=blue}`` - a trailing colour on a heading, paragraph,
        list item, to-do or quote.
      ``::: columns`` ... ``::: column`` ... ``:::`` - a column list, one
        column per ``::: column`` section (at least two).
      ``<!-- table: row_header -->`` - the next table's header flags
        (``row_header`` / ``no_row_header`` / ``column_header`` /
        ``no_column_header``). A table whose first row is blank gets a row
        header and no column header without a hint.

    Inline markdown (**bold**, *italic*, `code`, ~~strike~~, [label](url)) is
    converted to Notion rich-text annotations everywhere text is emitted.
    Blank lines are skipped. A directive that cannot be honoured raises
    ``MarkdownBlockError`` before anything is written.
    """
    blocks: list[dict] = []
    lines = text.split("\n")
    i = 0
    n = len(lines)
    table_hint: dict[str, bool] | None = None

    while i < n:
        content = lines[i].strip()
        if not content:
            i += 1
            continue

        if table_hint is not None and not _is_table_row(content):
            raise MarkdownBlockError(
                "`<!-- table: ... -->` must be followed by a Markdown table"
            )

        # --- Fenced code block ---
        if content.startswith("```"):
            language = content[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume the closing fence (no-op past EOF)
            blocks.append(_code_block("\n".join(code_lines), language))
            continue

        # --- Table header hint (applies to the next table) ---
        hint = _TABLE_HINT_RE.fullmatch(content)
        if hint is not None:
            table_hint = _parse_table_hint(hint.group(1))
            i += 1
            continue

        # --- Column layout (::: columns ... ::: column ... :::) ---
        if _COLUMNS_OPEN_RE.fullmatch(content):
            column_list, i = _column_list_block(lines, i + 1)
            blocks.append(column_list)
            continue
        if content == ":::" or _COLUMN_SPLIT_RE.fullmatch(content):
            raise MarkdownBlockError(
                f"`{content}` is outside a column layout: open it with `::: columns`"
            )

        # --- Table (consecutive pipe rows) ---
        if _is_table_row(content):
            raw_rows: list[list[str]] = []
            while i < n and _is_table_row(lines[i].strip()):
                row_line = lines[i].strip()
                if not _is_separator_row(row_line):
                    raw_rows.append(_parse_table_row(row_line))
                i += 1
            if raw_rows:
                blocks.append(_build_table_block(raw_rows, **(table_hint or {})))
            table_hint = None
            continue

        # --- Divider ---
        if content in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # --- Headings (Notion supports h1-h3; deeper collapse to h3) ---
        heading = _heading_from_line(content)
        if heading is not None:
            blocks.append(_heading_block(heading[0], heading[1]))
            i += 1
            continue

        # --- Callout (> [!...]) and the > lines that follow it ---
        callout = _CALLOUT_LINE_RE.match(content)
        if callout is not None:
            body_lines: list[str] = []
            i += 1
            while i < n:
                quoted = lines[i].lstrip()
                if not quoted.startswith(">") or _CALLOUT_LINE_RE.match(quoted.rstrip()):
                    break
                body_lines.append(quoted[2:] if quoted.startswith("> ") else quoted[1:])
                i += 1
            blocks.append(_callout_block(callout.group(1), callout.group(2), "\n".join(body_lines)))
            continue

        # --- Quote ---
        if content == ">" or content.startswith("> "):
            blocks.append(_quote_block(content[2:] if len(content) > 1 else ""))
            i += 1
            continue

        # --- Lists (bullets / numbered / to-dos, with one nesting level) ---
        if _parse_list_marker(content) is not None:
            list_items: list[tuple[int, str, str, bool]] = []
            while i < n:
                stripped = lines[i].strip()
                if not stripped:
                    break
                marker = _parse_list_marker(stripped)
                if marker is None:
                    break
                expanded = lines[i].expandtabs(4)
                indent = len(expanded) - len(expanded.lstrip(" "))
                block_type, item_text, checked = marker
                list_items.append((indent, block_type, item_text, checked))
                i += 1
            blocks.extend(_build_nested_list(list_items))
            continue

        # --- Paragraph (default) ---
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": _text_block_body(content),
            }
        )
        i += 1

    if table_hint is not None:
        raise MarkdownBlockError("`<!-- table: ... -->` must be followed by a Markdown table")
    return blocks


def _heading_block(heading_type: str, text: str) -> dict:
    return {
        "object": "block",
        "type": heading_type,
        heading_type: _text_block_body(text),
    }


def _quote_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": _text_block_body(text),
    }


# ── Callouts ────────────────────────────────────────────────────────────────
# ``> [!icon=🎯 color=blue_background] ...``: the GitHub alert form, with
# attributes. A kind gives the icon and colour an attribute does not set.
_CALLOUT_LINE_RE = re.compile(r">[ \t]?\[!([^\]]*)\][ \t]*(.*)\Z", re.DOTALL)
_CALLOUT_MARKER_RE = re.compile(r"\[!([^\]]*)\][ \t]*(.*)\Z", re.DOTALL)
_CALLOUT_KINDS: dict[str, dict[str, str]] = {
    "callout": {},
    "note": {"icon": "ℹ️", "color": "blue_background"},
    "tip": {"icon": "💡", "color": "green_background"},
    "important": {"icon": "❗", "color": "purple_background"},
    "warning": {"icon": "⚠️", "color": "yellow_background"},
    "caution": {"icon": "🛑", "color": "red_background"},
}


# Notion's callout ``icon`` is an object or absent, never null (Notion-Version
# 2025-09-03): a callout created without one gets the default icon, and an
# update cannot remove it.
CALLOUT_ICON_REMOVAL_REFUSAL = (
    "Notion does not allow removing a callout icon through the API; remove it in the Notion UI"
)
_NO_ICON_VALUES = frozenset({"none", "null", "no", "off", "false"})


def find_null_callout_icon(block: Any, _depth: int = 0) -> str | None:
    """The path of a callout ``icon`` set to null inside one block payload, or ``None``."""
    if _depth > 32 or not isinstance(block, dict):
        return None
    callout = block.get("callout")
    if isinstance(callout, dict) and "icon" in callout and callout["icon"] is None:
        return "callout.icon"
    for key, value in block.items():
        children = value.get("children") if isinstance(value, dict) else None
        if key == "children":
            children = value
        if not isinstance(children, list):
            continue
        prefix = "children" if key == "children" else f"{key}.children"
        for idx, child in enumerate(children):
            found = find_null_callout_icon(child, _depth + 1)
            if found:
                return f"{prefix}[{idx}].{found}"
    return None


def _parse_callout_marker(spec: str) -> dict:
    """The ``icon`` / ``color`` body fields a ``[!...]`` marker sets."""
    kind: str | None = None
    attrs: dict[str, str] = {}
    for token in spec.split():
        key, sep, value = token.partition("=")
        if sep:
            key = key.lower()
            if key not in ("icon", "color"):
                raise MarkdownBlockError(
                    f"`{key}` is not a callout attribute in `[!{spec}]`; a callout takes icon= and color="
                )
            if key in attrs:
                raise MarkdownBlockError(f"`{key}` is set twice in `[!{spec}]`")
            if key == "icon" and value.lower() in _NO_ICON_VALUES:
                raise MarkdownBlockError(f"`icon={value}` in `[!{spec}]`: {CALLOUT_ICON_REMOVAL_REFUSAL}")
            attrs[key] = value
        elif kind is None and token.lower() in _CALLOUT_KINDS:
            kind = token.lower()
        else:
            raise MarkdownBlockError(
                f"`{token}` in `[!{spec}]` is neither icon=, color= nor a callout kind "
                "(NOTE, TIP, IMPORTANT, WARNING, CAUTION)"
            )
    if kind is None and not attrs:
        raise MarkdownBlockError(
            "`[!]` names no callout: write `[!icon=EMOJI color=COLOUR]` or `[!NOTE]`"
        )
    merged = {**_CALLOUT_KINDS.get(kind or "callout", {}), **attrs}
    body: dict = {}
    if "icon" in merged:
        body["icon"] = _notion_icon(merged["icon"])
    if "color" in merged:
        body["color"] = _notion_color(merged["color"])
    return body


def _callout_block(spec: str, rest: str, body_markdown: str) -> dict:
    """A callout from its marker, the text after it and the Markdown of its ``>`` lines."""
    callout: dict[str, Any] = {"rich_text": [], **_parse_callout_marker(spec)}
    children = _text_to_blocks(body_markdown) if body_markdown.strip() else []
    heading = _heading_from_line(rest.strip()) if rest.strip() else None
    if heading is not None:
        children.insert(0, _heading_block(*heading))
    elif rest.strip():
        callout["rich_text"] = _parse_inline_markdown(rest.strip())
    if children:
        callout["children"] = children
    return {"object": "block", "type": "callout", "callout": callout}


def _update_block_fields(block_type: str, text: str) -> tuple[str, dict]:
    """The text an update writes to a block and the block fields it sets.

    The read tools write a block's colour as a trailing ``{color=...}`` and a
    callout's icon and colour as its ``> [!...]`` marker
    (``_common._block_to_text``); written back, they set the block's colour
    and icon instead of landing in its text.
    """
    if block_type == "callout":
        marker_text = text.lstrip()
        if marker_text.startswith(">"):
            marker_text = marker_text[1:].lstrip()
        marker = _CALLOUT_MARKER_RE.match(marker_text)
        if marker is None:
            return text, {}
        return marker.group(2), _parse_callout_marker(marker.group(1))
    content, attrs = _split_block_attrs(text)
    return content, _block_attrs_body(attrs, allow_icon=False)


# ── Column layouts ──────────────────────────────────────────────────────────
_COLUMNS_OPEN_RE = re.compile(r":::[ \t]*columns", re.IGNORECASE)
_COLUMN_SPLIT_RE = re.compile(r":::[ \t]*column", re.IGNORECASE)


def _column_list_block(lines: list[str], start: int) -> tuple[dict, int]:
    """The column list opened by ``::: columns`` above *start*, and the line after it.

    ``::: column`` starts each column (the first one may be implicit) and
    ``:::`` closes the layout; a missing ``:::`` closes it at the end of the
    text. Fenced code inside a column is skipped, so a ``:::`` in a code
    sample is not read as a fence.
    """
    columns: list[list[str]] = [[]]
    explicit = False
    in_code = False
    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
        elif not in_code:
            if stripped == ":::":
                i += 1
                break
            if _COLUMNS_OPEN_RE.fullmatch(stripped):
                raise MarkdownBlockError(
                    "a column layout cannot hold another one: close it with `:::` first"
                )
            if _COLUMN_SPLIT_RE.fullmatch(stripped):
                if explicit or any(part.strip() for part in columns[-1]):
                    columns.append([])
                explicit = True
                i += 1
                continue
        columns[-1].append(line)
        i += 1
    if len(columns) < 2:
        raise MarkdownBlockError(
            "a `::: columns` layout needs at least two columns: start each one with `::: column`"
        )
    column_blocks = []
    for column_lines in columns:
        children = _text_to_blocks("\n".join(column_lines)) or [
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}
        ]
        column_blocks.append({"object": "block", "type": "column", "column": {"children": children}})
    return (
        {"object": "block", "type": "column_list", "column_list": {"children": column_blocks}},
        i,
    )


def _summarize_properties(
    props: dict,
    resolved_relations: dict[str, str] | None = None,
) -> str:
    """Summarize database entry properties as a compact string.

    ``resolved_relations`` is an optional mapping of relation page-ID → title
    so that relation properties display human-readable titles instead of UUIDs.
    Each value is read by ``_property_value_json`` (the one per-type table the
    JSON format shares) and written for a reader by ``_property_text``.
    """
    parts = []
    for name, prop in props.items():
        value = _property_text(prop, resolved_relations)
        if value:
            parts.append(f"{name}: {value}")

    return " | ".join(parts) if parts else "No properties"


def _property_text(
    prop: dict,
    resolved_relations: dict[str, str] | None = None,
    *,
    in_rollup: bool = False,
) -> str:
    """One property as the text format shows it: its JSON value, for a reader.

    Only the presentation lives here - a checkbox as ✓/✗, a date by its start,
    a person by name or ``@Anonymous`` (``_person_label``), a related page by
    its title (or 8 characters of its id inside a rollup), the first 10 items
    of a rollup array. What a property holds is ``_property_value_json``.
    """
    prop_type = prop.get("type", "")
    raw = prop.get(prop_type)
    if prop_type == "people":
        return ", ".join(_person_label(p) for p in raw or [])
    if prop_type in ("created_by", "last_edited_by"):
        return _person_label(raw, "") if raw else ""
    if prop_type == "rollup":
        rollup = raw if isinstance(raw, dict) else {}
        rtype = rollup.get("type", "")
        if rtype == "incomplete":
            return "(computing…)"
        if rtype == "unsupported":
            return "(unsupported rollup)"
        if rtype == "array":
            items = [i for i in (rollup.get("array") or [])[:10] if isinstance(i, dict)]
            return ", ".join(
                text for text in (_property_text(i, in_rollup=True) for i in items) if text
            )
    value = _property_value_json(prop)
    if prop_type == "checkbox":
        return "✓" if value else "✗"
    if prop_type == "relation":
        ids = [i for i in value if i]
        if in_rollup:
            return ", ".join(i[:8] for i in ids)
        if resolved_relations:
            return ", ".join(resolved_relations.get(i, i[:8]) for i in ids)
        return ", ".join(ids)
    if isinstance(value, dict):  # a date, read by its start
        return value.get("start") or ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return "" if value is None else str(value)


def _list_empty_properties(props: dict) -> str:
    """Return a summary of properties that exist in the schema but have no value.

    Skips computed/auto-set property types (title, created_time, etc.) and
    checkbox (always has a value).  The result looks like:
    ``Product Owner (people), Vertical (select), UC/EPIC (relation)``
    """
    # Property types that always have a value (computed or auto-set)
    always_filled = {
        "title",
        "created_time",
        "last_edited_time",
        "created_by",
        "last_edited_by",
        "checkbox",
        "unique_id",
    }
    empty: list[str] = []
    for name, prop in props.items():
        prop_type = prop.get("type", "")
        if prop_type in always_filled:
            continue
        has_value = False
        if prop_type == "rich_text":
            has_value = bool(_rich_text_to_plain(prop.get("rich_text", [])))
        elif prop_type == "number":
            has_value = prop.get("number") is not None
        elif prop_type == "select":
            has_value = prop.get("select") is not None
        elif prop_type == "multi_select":
            has_value = bool(prop.get("multi_select"))
        elif prop_type == "date":
            has_value = prop.get("date") is not None
        elif prop_type == "url":
            has_value = bool(prop.get("url"))
        elif prop_type == "email":
            has_value = bool(prop.get("email"))
        elif prop_type == "status":
            has_value = prop.get("status") is not None
        elif prop_type == "people":
            has_value = bool(prop.get("people"))
        elif prop_type == "phone_number":
            has_value = bool(prop.get("phone_number"))
        elif prop_type == "relation":
            has_value = bool(prop.get("relation"))
        elif prop_type == "rollup":
            rollup = prop.get("rollup") or {}
            rtype = rollup.get("type", "")
            if rtype == "number":
                has_value = rollup.get("number") is not None
            elif rtype == "date":
                has_value = rollup.get("date") is not None
            elif rtype == "array":
                has_value = bool(rollup.get("array"))
            else:
                has_value = rollup.get(rtype) is not None
        elif prop_type == "formula":
            has_value = True  # Computed
        elif prop_type == "files":
            has_value = bool(prop.get("files"))
        else:
            has_value = True  # Unknown type — don't flag

        if not has_value:
            empty.append(f"{name} ({prop_type})")

    return ", ".join(empty) if empty else ""


def _person_json(user: object) -> dict | None:
    """A people value as ``{"id", "name"}``; the name is ``None`` when withheld."""
    if not isinstance(user, dict):
        return None
    name = user.get("name")
    return {
        "id": user.get("id"),
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
    }


def _date_json(date: object) -> dict | None:
    """A date value with both ends and its time zone, ``None`` when unset."""
    if not isinstance(date, dict):
        return None
    return {
        "start": date.get("start"),
        "end": date.get("end"),
        "time_zone": date.get("time_zone"),
    }


def _property_value_json(prop: dict) -> Any:
    """One property's value as plain JSON, for a caller that reads it as data.

    The value keeps what ``_summarize_properties`` drops for a reader: the end
    of a date range, the type of a number or a boolean, the id of a person or
    of a related page, an empty value as ``None`` (or ``[]`` for a list).
    """
    prop_type = prop.get("type", "")
    raw = prop.get(prop_type)
    if prop_type in ("title", "rich_text"):
        return _rich_text_to_plain(raw or [])
    if prop_type in ("select", "status"):
        return raw.get("name") if isinstance(raw, dict) else None
    if prop_type == "multi_select":
        return [s.get("name") for s in raw or [] if isinstance(s, dict)]
    if prop_type == "date":
        return _date_json(raw)
    if prop_type == "people":
        return [p for p in (_person_json(u) for u in raw or []) if p]
    if prop_type in ("created_by", "last_edited_by"):
        return _person_json(raw)
    if prop_type == "relation":
        return [r.get("id") for r in raw or [] if isinstance(r, dict)]
    if prop_type == "files":
        return [f.get("name") for f in raw or [] if isinstance(f, dict)]
    if prop_type == "formula":
        formula = raw if isinstance(raw, dict) else {}
        ftype = formula.get("type", "")
        value = formula.get(ftype)
        return _date_json(value) if ftype == "date" else value
    if prop_type == "rollup":
        rollup = raw if isinstance(raw, dict) else {}
        rtype = rollup.get("type", "")
        if rtype == "array":
            return [_property_value_json(item) for item in rollup.get("array") or []]
        if rtype == "date":
            return _date_json(rollup.get("date"))
        return rollup.get(rtype)
    if prop_type == "unique_id":
        uid = raw if isinstance(raw, dict) else {}
        num = uid.get("number")
        prefix = uid.get("prefix")
        return f"{prefix}-{num}" if prefix and num is not None else num
    if prop_type == "verification":
        return raw.get("state") if isinstance(raw, dict) else None
    # number, checkbox, url, email, phone_number, created_time,
    # last_edited_time and any type added later carry a JSON value as is.
    return raw


def _page_row_json(page: dict) -> dict:
    """A database row as data: its identity, timestamps and property values.

    Notion sends at most 25 references of a relation, people, title or rich
    text property in a row and flags the rest with ``has_more``: those
    properties are named in ``truncated`` so a reader never takes a partial
    list for the whole one (``get_notion_page_property`` reads them all).
    """
    properties = page.get("properties") or {}
    with unnamed_people_not_collected():
        values = {
            name: _property_value_json(prop)
            for name, prop in properties.items()
            if isinstance(prop, dict)
        }
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "in_trash": bool(page.get("in_trash") or page.get("archived")),
        "properties": values,
        "truncated": sorted(
            name
            for name, prop in properties.items()
            if isinstance(prop, dict) and prop.get("has_more")
        ),
    }
