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

"""Split one tool result into workset items, without the model copying it.

``to_workset`` (``app/tools/base.py``) sends a read tool's full result here
instead of into the model's context: the text is cut into items by a rule the
agent states, each text item keeps its exact characters (a JSON item is its
record, re-serialised), and only a receipt goes back.
Nothing here knows a source: the agent describes the result's shape (a JSON
list, or the line each item starts with) and where an item's reference, date
and link sit.

Every pattern comes from the model and runs on text a third party wrote, so
they run on the bounded regex engine within one budget for the whole split
(``SPLIT_REGEX_BUDGET_SECONDS``), and a pattern, a result and an item count
are capped. A split that cannot finish says why and ingests nothing: a half
corpus that looks whole is the failure this feature exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.content_safety import untrusted_block_spans
from app.services.schema_guard import bounded_regex

#: CPU-bounded time for every pattern of one split together.
SPLIT_REGEX_BUDGET_SECONDS = 2.0
#: Longest pattern accepted.
MAX_PATTERN_CHARS = 300
#: Largest result split (characters); a larger one is refused, not cut.
MAX_RESULT_CHARS = 5_000_000
#: Most items one split may produce.
MAX_ITEMS = 500
#: Longest title kept.
MAX_TITLE_CHARS = 300
#: Start of the preamble kept for the receipt.
MAX_PREAMBLE_CHARS = 600

DATE_ORDERS = ("YMD", "DMY", "MDY")

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_MARKUP = re.compile(r"^[#>*\-\s|`_]+|[*`_]+$")
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_NUMERIC = re.compile(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})")


class SplitError(ValueError):
    """The split asked for is invalid (or over a bound); nothing is ingested.

    A split that is valid but finds nothing to cut (a JSON split of a text
    result, an item_start matching no line) is not an error: the result is
    stored whole as one item and :attr:`SplitResult.fallback` says why.
    """


@dataclass
class ItemDraft:
    """One item cut from a tool result: its exact text, or its JSON record."""

    title: str
    content: str
    external_ref: str
    occurred_at: datetime | None = None
    source_url: str | None = None
    ref_is_digest: bool = False


@dataclass
class SplitResult:
    items: list[ItemDraft] = field(default_factory=list)
    #: Characters before the first item, not ingested (for JSON: the scalar
    #: values next to the list).
    preamble_chars: int = 0
    #: The start of that preamble (a header, a total, a page count), for the receipt.
    preamble: str = ""
    #: Items left out, with why (empty, duplicate reference in this result).
    skipped: list[str] = field(default_factory=list)
    #: Dates that could not be read, by item title.
    undated: list[str] = field(default_factory=list)
    #: Why the split asked for could not apply, the whole result being stored as
    #: one item instead (a JSON split of a text result, an item_start matching
    #: nothing): the call is not wasted and the receipt says so. Empty when the
    #: split applied as asked.
    fallback: str = ""


@dataclass
class SplitSpec:
    item_start: str | None = None
    json_path: str | None = None
    title_pattern: str | None = None
    ref_pattern: str | None = None
    date_pattern: str | None = None
    url_pattern: str | None = None
    title_field: str | None = None
    ref_field: str | None = None
    date_field: str | None = None
    url_field: str | None = None
    date_order: str = "YMD"

    @classmethod
    def from_argument(cls, raw: Any) -> SplitSpec:
        """The spec ``to_workset`` carries: ``true`` (one item) or an object."""
        if raw is True or raw in ("", None):
            return cls()
        if not isinstance(raw, dict):
            raise SplitError("to_workset must be true or an object describing how to split the result.")
        unknown = sorted(set(raw) - set(cls.__dataclass_fields__))
        if unknown:
            raise SplitError(f"to_workset does not take {', '.join(unknown)}.")
        spec = cls(**{k: (str(v) if v is not None else None) for k, v in raw.items()})
        spec.date_order = (spec.date_order or "YMD").upper()
        if spec.date_order not in DATE_ORDERS:
            raise SplitError(f"date_order must be one of {', '.join(DATE_ORDERS)}.")
        for name in ("item_start", "title_pattern", "ref_pattern", "date_pattern", "url_pattern"):
            pattern = getattr(spec, name)
            if pattern is not None and len(pattern) > MAX_PATTERN_CHARS:
                raise SplitError(f"{name} is longer than {MAX_PATTERN_CHARS} characters.")
        return spec


class _Budget:
    """One regex budget for a whole split, on the bounded engine."""

    def __init__(self, seconds: float | None = None) -> None:
        if bounded_regex is None:
            raise SplitError("Pattern splitting is unavailable on this platform.")
        self._deadline = time.monotonic() + (SPLIT_REGEX_BUDGET_SECONDS if seconds is None else seconds)

    def remaining(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise SplitError("Splitting did not finish within its time budget; use simpler patterns.")
        return left

    def compile(self, pattern: str, *, multiline: bool = False) -> Any:
        try:
            re.compile(pattern)
            return bounded_regex.compile(pattern, bounded_regex.MULTILINE if multiline else 0)
        except (re.error, bounded_regex.error) as exc:
            raise SplitError(f"Invalid pattern {pattern!r}: {exc}.") from None

    def starts(self, compiled: Any, text: str) -> list[int]:
        try:
            return [m.start() for m in compiled.finditer(text, timeout=self.remaining())]
        except TimeoutError:
            raise SplitError("Splitting did not finish within its time budget; use simpler patterns.") from None

    def first(self, compiled: Any, text: str) -> str | None:
        try:
            match = compiled.search(text, timeout=self.remaining())
        except TimeoutError:
            raise SplitError("Splitting did not finish within its time budget; use simpler patterns.") from None
        if match is None:
            return None
        value = match.group(1) if match.groups() else match.group(0)
        return value.strip() if value else None


def parse_date(raw: str | None, order: str) -> datetime | None:
    """A date read in a source, in the order the source writes it; ``None`` when unreadable.

    A numeric date is never guessed: ``10/6/2026`` is the 6th of October only
    when the agent says the source writes month first.
    """
    if not raw:
        return None
    text = raw.strip()
    iso = _ISO.match(text)
    try:
        if iso:
            year, month, day = (int(g) for g in iso.groups())
        else:
            numeric = _NUMERIC.match(text)
            if numeric is None:
                return None
            a, b, c = (int(g) for g in numeric.groups())
            if order == "YMD":
                year, month, day = a, b, c
            elif order == "DMY":
                day, month, year = a, b, c
            else:
                month, day, year = a, b, c
        if year < 100:
            return None
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _title_of(text: str) -> str:
    for line in text.splitlines():
        cleaned = _MARKUP.sub("", line).strip()
        if cleaned:
            return cleaned[:MAX_TITLE_CHARS]
    return "Untitled"


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _dig(value: Any, path: str | None) -> Any:
    if not path:
        return value
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


_JSON_FIELDS = ("title_field", "ref_field", "date_field", "url_field")


def _wants_json(spec: SplitSpec) -> bool:
    """Whether *spec* asks for a JSON split: a ``json_path`` (``""`` is the top level) or an item field."""
    return spec.json_path is not None or any(getattr(spec, name) for name in _JSON_FIELDS)


def _json_context(data: Any, path: str) -> str:
    """The scalar values next to the list on its path (a total, a page count, a cursor), as ``key: value``."""
    parts: list[str] = []
    node = data
    for key in [*(path.split(".") if path else []), None]:
        if isinstance(node, dict):
            parts.extend(
                f"{name}: {json.dumps(value, ensure_ascii=False, default=str)}"
                for name, value in node.items()
                if name != key and not isinstance(value, (dict, list))
            )
        if key is None:
            break
        node = _dig(node, key)
    return ", ".join(parts)


def _split_json(data: Any, spec: SplitSpec) -> list[tuple[str, Any]]:
    items = _dig(data, spec.json_path)
    if not isinstance(items, list):
        where = f"at '{spec.json_path}'" if spec.json_path else "at the top level"
        raise SplitError(f"The result holds no JSON list {where}.")
    return [(json.dumps(item, ensure_ascii=False, indent=1, default=str), item) for item in items]


def _fenced_json(text: str) -> tuple[Any, str] | None:
    """The JSON body of a result that fences it, with the text around the fence.

    An integration answers a status line, then its body in one quarantine
    block (:func:`app.services.content_safety.wrap_untrusted`): the whole text
    is not JSON, the block's data is. ``(data, outside)`` when *text* holds
    exactly one such block and its data parses as a JSON object or list;
    ``None`` otherwise (no block, several, a memory block, a cut body).
    """
    spans = untrusted_block_spans(text)
    if len(spans) != 1:
        return None
    start, end = spans[0]
    block = text[start:end]
    marker = block.find("\n<<UNTRUSTED-")
    if marker < 0:
        return None
    # The opening marker line, then the [source: ...] line, then the data.
    _, _, inside = block[marker + 1 :].partition("\n")
    _, _, inside = inside.partition("\n")
    body = inside.rsplit("\n<<END-UNTRUSTED-", 1)[0].strip()
    if not body or body[0] not in "[{":
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    outside = " ".join(part for part in (text[:start].strip(), text[end:].strip()) if part)
    return data, outside


def split_result(content: str, spec: SplitSpec, structured: Any = None) -> SplitResult:
    """Cut *content* into items as *spec* says; raises :class:`SplitError` on any doubt.

    *structured* is the JSON value the tool returned beside its text
    (``ToolResult.structured``): a JSON split of a text result splits that
    value instead, so a tool that writes text for the model and records for
    a reader is split one item per record, never stored whole.
    """
    if len(content) > MAX_RESULT_CHARS:
        raise SplitError(f"The result is {len(content):,} characters, over the {MAX_RESULT_CHARS:,} a split accepts.")
    budget = _Budget()
    result = SplitResult()
    pieces: list[tuple[str, Any]] = []

    stripped = content.strip()
    if _wants_json(spec):
        if spec.item_start is not None:
            raise SplitError("Give either a JSON split (json_path, *_field) or item_start, not both.")
        outside = ""
        parsed = True
        try:
            as_json = json.loads(stripped)
        except ValueError:
            as_json, parsed = None, False
        if not parsed and (fenced := _fenced_json(stripped)) is not None:
            (as_json, outside), parsed = fenced, True
        if not parsed and isinstance(structured, (dict, list)):
            as_json, parsed = structured, True
        if not parsed:
            result.fallback = (
                "a JSON split was asked for but the result is not JSON (it is text): "
                "it is stored whole as one item; split such a result with item_start."
            )
            pieces = [(stripped, None)]
        if not result.fallback:
            pieces = _split_json(as_json, spec)
            context = "; ".join(part for part in (outside, _json_context(as_json, spec.json_path or "")) if part)
            result.preamble_chars = len(context)
            result.preamble = context[:MAX_PREAMBLE_CHARS]
    elif spec.item_start:
        starts = budget.starts(budget.compile(spec.item_start, multiline=True), content)
        if starts:
            preamble = content[: starts[0]].strip()
            result.preamble_chars = len(preamble)
            result.preamble = preamble[:MAX_PREAMBLE_CHARS]
            bounds = [*starts, len(content)]
            pieces = [(content[bounds[i] : bounds[i + 1]].strip(), None) for i in range(len(starts))]
        else:
            result.fallback = (
                "item_start matched no line of the result: it is stored whole as one item; "
                "read one small result first and write item_start for its first line exactly as it renders."
            )
            pieces = [(stripped, None)]
    else:
        pieces = [(stripped, None)]

    if len(pieces) > MAX_ITEMS:
        raise SplitError(f"The result splits into {len(pieces)} items, over the {MAX_ITEMS} one call may ingest.")

    # Multiline, as for item_start: a field is usually "the line that starts with ...".
    compiled = {
        name: budget.compile(pattern, multiline=True)
        for name in ("title_pattern", "ref_pattern", "date_pattern", "url_pattern")
        if (pattern := getattr(spec, name))
    }
    seen: set[str] = set()
    for text, record in pieces:
        if not text:
            result.skipped.append("an empty item")
            continue

        def field_value(kind: str, text: str = text, record: Any = record) -> str | None:
            if record is not None and (path := getattr(spec, f"{kind}_field")):
                value = _dig(record, path)
                return None if value is None else str(value).strip() or None
            pattern = compiled.get(f"{kind}_pattern")
            return budget.first(pattern, text) if pattern is not None else None

        ref = field_value("ref")
        title = field_value("title") or (
            # A JSON record's first line is "{": name it by its place instead.
            _title_of(text) if record is None else f"Item {ref or len(result.items) + len(result.skipped) + 1}"
        )
        title = title[:MAX_TITLE_CHARS]
        ref_is_digest = ref is None
        if ref is None:
            ref = _digest(text)
        if ref in seen:
            result.skipped.append(f"'{title}' (its reference {ref} appears twice in this result)")
            continue
        seen.add(ref)
        raw_date = field_value("date")
        occurred_at = parse_date(raw_date, spec.date_order)
        if raw_date and occurred_at is None:
            result.undated.append(title)
        url = field_value("url")
        if url is None and record is None:
            found = _URL.search(text)
            url = found.group(0) if found else None
        result.items.append(
            ItemDraft(
                title=title,
                content=text,
                external_ref=ref,
                occurred_at=occurred_at,
                source_url=url,
                ref_is_digest=ref_is_digest,
            )
        )
    return result
