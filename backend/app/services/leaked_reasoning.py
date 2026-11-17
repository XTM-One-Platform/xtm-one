# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Keep a model's reasoning out of the prompts XTM One builds.

Claude declines a prompt that carries a model's reasoning back to it
(``stop_details.category: reasoning_extraction``), and that decline is never
failed over. The tags are those models write their reasoning in today
(:data:`REASONING_TAGS`): another provider's tag is one entry away. Memory strips
the reasoning from what it stores and recalls; the trace tools mark it omitted
in what they hand an agent, while the stored trace keeps it.
"""

from __future__ import annotations

import re
from typing import Any

#: The tags a model wraps its reasoning in when it writes it as text.
REASONING_TAGS = ("reasoning", "thinking", "think")

#: What the trace tools show where reasoning was left out, so an agent
#: debugging a decline sees that something was there.
REASONING_OMITTED = "[reasoning omitted]"

_TAG_RE = re.compile(rf"<(/?)({'|'.join(REASONING_TAGS)})>", re.IGNORECASE)

_TRUNCATION_MARKER = r"… \[truncated \d+ chars\]\Z"

#: A ``"reasoning"`` / ``"thinking"`` string member's value in serialized JSON,
#: possibly cut short by a truncated excerpt, then stopping at its marker.
_JSON_VALUE = rf'"((?:[^"\\]|\\.)*?)(?:"|(?={_TRUNCATION_MARKER})|\Z)'
_JSON_MEMBER_RES = {
    key: re.compile(rf'("{key}"\s*:\s*){_JSON_VALUE}', re.DOTALL)
    for key in ("reasoning", "thinking")
}


def _blocks(text: str, *, leading: bool) -> list[tuple[int, int]]:
    """The spans of closed reasoning blocks in *text*, trailing whitespace included.

    One pass pairs each closing tag with the innermost open one of its name, so
    reasoning that quotes its own tag is removed whole, and an opening never
    closed (a prompt naming the tag) leaves the later blocks matchable. Only the
    outermost pairs are kept. *leading* keeps the blocks the text starts with.
    """
    open_at: dict[str, list[int]] = {}
    pairs: list[tuple[int, int, str]] = []
    for token in _TAG_RE.finditer(text):
        tag = token.group(2).lower()
        stack = open_at.setdefault(tag, [])
        if not token.group(1):
            stack.append(token.start())
        elif stack:
            pairs.append((stack.pop(), token.end(), tag))
    pairs.sort()
    lead = len(text) - len(text.lstrip())
    # Text that opens with a tag left unmatched is reasoning naming its own tag:
    # its close paired with the mention, so the first pair of that name starts here.
    for tag, unmatched in open_at.items():
        if unmatched and unmatched[0] == lead:
            first = next((i for i, pair in enumerate(pairs) if pair[2] == tag), None)
            if first is not None:
                pairs[first] = (lead, pairs[first][1], tag)
                pairs.sort()
            break
    spans: list[tuple[int, int]] = []
    pos = lead if leading else 0
    for start, close, _tag in pairs:
        if start < pos:
            continue  # inside a block already kept, or overlapping it
        if leading and start != pos:
            break
        end = close
        while end < len(text) and text[end].isspace():
            end += 1
        spans.append((start, end))
        pos = end
    return spans


def _without_spans(text: str, spans: list[tuple[int, int]], marker: str) -> str:
    if not spans:
        return text
    parts, pos = [], 0
    for start, end in spans:
        parts.append(text[pos:start])
        if marker:
            parts.append(marker + " ")
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def strip_leading_reasoning(text: str) -> str:
    """*text* without the closed reasoning blocks it starts with."""
    if not text or "<" not in text:
        return text
    spans = _blocks(text, leading=True)
    return text[spans[-1][1] :] if spans else text


def strip_reasoning_blocks(text: str, *, marker: str = "") -> str:
    """*text* with every closed reasoning block, wherever it sits, replaced by *marker*.

    A recalled leak sits mid-text in a trace, after the memory preamble that
    introduced it.
    """
    if not text or "<" not in text:
        return text
    return _without_spans(text, _blocks(text, leading=False), marker)


def _reasoning_key(value: dict) -> str | None:
    """The key holding a model's own reasoning in *value*, if it has one.

    A model call's output is ``{"content", "reasoning"}``; a chat message
    replays its ``thinking`` next to its ``role``. A ``reasoning`` field
    anywhere else is visible content (a judge's verdict, a tool argument).
    """
    if "content" in value and isinstance(value.get("reasoning"), str):
        return "reasoning"
    if "role" in value and isinstance(value.get("thinking"), str):
        return "thinking"
    return None


def without_reasoning(value: Any) -> Any:
    """A payload as an agent may read it: the model's reasoning marked omitted."""
    if isinstance(value, dict):
        omitted = _reasoning_key(value)
        return {
            key: REASONING_OMITTED if key == omitted else without_reasoning(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [without_reasoning(item) for item in value]
    if isinstance(value, str):
        return strip_reasoning_blocks(value, marker=REASONING_OMITTED)
    return value


def serialized_without_reasoning(text: str) -> str:
    """:func:`without_reasoning` for a serialized JSON excerpt that can no longer be parsed.

    Such as the excerpt an offloaded trace payload keeps, cut wherever its size
    limit fell. The same two shapes are matched as on a parsed payload: a model
    call's output starts with ``{"content"``, a chat message carries ``"role"``.
    """
    if not text:
        return text
    if text.startswith('{"content"'):
        text = _JSON_MEMBER_RES["reasoning"].sub(rf'\1"{REASONING_OMITTED}"', text)
    if '"role"' in text:
        text = _JSON_MEMBER_RES["thinking"].sub(rf'\1"{REASONING_OMITTED}"', text)
    return strip_reasoning_blocks(text, marker=REASONING_OMITTED)
