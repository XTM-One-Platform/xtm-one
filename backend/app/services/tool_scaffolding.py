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

"""Fold narrated tool-call scaffolding out of a delivered answer.

A model that hits a refused tool call sometimes narrates a fabricated
transcript of it into its FINAL answer text - a bare tool name, a ``Request``
label, the JSON arguments, then a ``Tool Call Failed: ...`` line listing tools
that exist nowhere - and the background-task delivery surfaced that text
verbatim into the parent conversation.  The renderer is correct; the content
is bad.  Per the repository invariant (deterministic code over prompt text)
this module removes those blocks from the text that is DELIVERED, instead of
asking the model not to write them.  The raw transcript is never touched: the
run detail keeps every real tool call and result.

The grammar is deliberately narrow so a real deliverable is never cut.  A
block is folded only when ALL of this holds:

* a line that is ONLY a lowercase snake_case tool name (optionally wrapped in
  backticks / emphasis / brackets, under a heading marker, or prefixed with
  ``Tool:`` / ``Tool call:`` / ``Tool name:``),
* immediately followed by a bare ``Request`` / ``Arguments`` / ``Input`` /
  ``Parameters`` label,
* followed by a JSON object - on the label's line, on the next line, or
  inside a code fence - and nothing but closing markup after it on its last
  line (``Request: {"id": "x"} - I could not update it`` is a sentence, and
  its conclusion is kept),
* and the tool name is one the run actually knows - a name the model called
  (``known_tools``, see :func:`transcript_tool_names`) - OR the block carries
  an explicit ``Tool Call Failed`` line.  A snake_case identifier followed by
  ``Request`` / ``Response`` JSON is also the shape of a legitimate API
  write-up (``create_user`` + payloads); the vocabulary check is what tells
  the two apart.  A caller that passes ``known_tools=None`` opts out of the
  check and folds every well-formed block.

Trailing ``Response`` / ``Result`` / ``Output`` / ``Error`` sections whose
payload is a JSON object, and ``Tool Call Failed`` lines, are folded with the
block.  A standalone ``Tool Call Failed: ...`` line is folded on its own only
when it has the transcript shape - it says ``not found`` or lists
``Available tools:`` - so a sentence that merely starts with those words
(``Tool Call Failed: Tool 'x' is not available to this agent, so I ...``) is
kept: that is the honest report the refusal asks the model for.  A
``Response`` label followed by prose keeps the prose - a model's real
conclusion can sit right there, and one stray label costs less than a lost
answer.

Every scan is bounded (``_MAX_NAME_LINE_CHARS``, ``_MAX_PAYLOAD_LINES``,
``_MAX_SCAN_LINES_TOTAL``): the fold runs on the worker's event loop right
after the agentic loop returns, and a degenerate answer (a model looping on an
unbalanced ``{``, a 100k-character run of ``*``) must cost milliseconds, not
the tens of seconds an unbounded scan took.  Past the total budget the rest of
the text is kept as-is - a bounded miss beats a stalled worker.

This is applied to background sub-agent results only.  A ``consult_agent``
answer is a tool result the PARENT model reads and rewrites before anything
reaches the user, and a ``transfer_to_agent`` answer is an ordinary chat turn
streamed like any other; neither bypasses a model the way the completion
notice and the parent wake do, so neither is folded.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

#: What the delivery says when the WHOLE answer was scaffolding: an empty
#: result would read as "the agent did nothing", which is not what happened.
SCAFFOLDING_ONLY_NOTICE = (
    "The agent's answer contained only a narrated tool-call transcript{tools} and no other content."
)

#: A tool-name line is short by construction (a sanitised tool name is at most
#: 64 characters, plus a little markup or a ``Tool:`` prefix).  Longer lines
#: are never candidates - and skipping them keeps ``_NAME_LINE_RE``, whose two
#: adjacent optional markup runs backtrack quadratically, off inputs where a
#: 100k-character run of ``*`` took 24 s.
_MAX_NAME_LINE_CHARS = 200
#: A narrated JSON payload spans a handful of lines.  The closing-brace scan
#: stops after this many, so an unbalanced ``{`` cannot drag every later
#: candidate through the whole answer (20 s on a 200 KB degenerate input).
_MAX_PAYLOAD_LINES = 120
#: Hard ceiling on the lines one fold may scan for closing braces in total.
_MAX_SCAN_LINES_TOTAL = 200_000

_MARKUP = r"[`*_\[\]]*"
_NAME_LINE_RE = re.compile(
    rf"^\s*(?:#{{1,6}}\s+)?{_MARKUP}(?i:tool(?:\s*(?:call|name))?\s*:\s*)?{_MARKUP}"
    r"(?P<name>[a-z][a-z0-9]*(?:_[a-z0-9]+)*)"
    rf"{_MARKUP}\s*:?\s*$"
)
# ``rest`` is captured greedily to the end of the line (linear) and stripped
# in code: a lazy ``.*?`` followed by ``\s*$`` is quadratic on a long tail.
_LABEL_TAIL = rf"\b{_MARKUP}\s*:?\s*(?P<rest>.*)$"
_REQUEST_LINE_RE = re.compile(rf"^\s*{_MARKUP}(?i:request|arguments|args|input|inputs|parameters|params){_LABEL_TAIL}")
_RESPONSE_LINE_RE = re.compile(rf"^\s*{_MARKUP}(?i:response|result|results|output|error){_LABEL_TAIL}")
_FAILED_LINE_RE = re.compile(r"^\s*[`*_\[]*(?i:tool\s+call\s+failed)\b")
#: The transcript shape of a failure line: a "not found" verdict or a tool
#: listing.  A standalone failure line without either is a sentence.
_FAILED_MARKER_RE = re.compile(r"\bnot\s+found\b|\b(?i:available\s+tools)\s*:")
_FAILED_TOOL_RE = re.compile(r"[Tt]ool\s+'(?P<name>[\w.\-]{1,128})'")
_FENCE_RE = re.compile(r"^\s*```")
_MANY_BLANK_LINES_RE = re.compile(r"(\r?\n)(?:\r?\n){2,}")
#: What may follow a JSON payload on its closing line: whitespace, closing
#: markup and end-of-sentence punctuation.  Anything else is prose.
_PAYLOAD_TAIL_CHARS = " \t\r`*_.,;"


@dataclass(frozen=True, slots=True)
class ScaffoldingFold:
    """The outcome of :func:`fold_tool_scaffolding`."""

    #: The answer with the scaffolding blocks removed.  Unchanged (byte for
    #: byte) when nothing was folded; may be empty when nothing else was there.
    text: str
    #: How many blocks were folded.
    removed_blocks: int
    #: The tool names the folded blocks named, first occurrence first.
    tool_names: tuple[str, ...]

    def delivered(self) -> str:
        """The text to deliver: the cleaned answer, or the notice when the
        answer held nothing but scaffolding."""
        if self.text or not self.removed_blocks:
            return self.text
        tools = f" ({', '.join(self.tool_names)})" if self.tool_names else ""
        return SCAFFOLDING_ONLY_NOTICE.format(tools=tools)


def transcript_tool_names(messages: Iterable[object]) -> frozenset[str]:
    """Every tool name a run's transcript touched.

    The names the model called (``tool_calls`` on assistant messages, in the
    exact form it wrote them - a refused call is still a call) plus the names
    on the tool results.  This is the vocabulary :func:`fold_tool_scaffolding`
    checks a narrated block against: a block naming a tool the run never
    touched, with no failure marker, is left alone.  Duck-typed over
    ``ChatMessage`` / ``ToolCall`` so it never imports the LLM layer.
    """
    names: set[str] = set()
    for message in messages:
        for call in getattr(message, "tool_calls", None) or ():
            name = getattr(call, "name", None)
            if isinstance(name, str) and name:
                names.add(name)
        if getattr(message, "role", None) == "tool":
            name = getattr(message, "name", None)
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


def fold_tool_scaffolding(
    text: str,
    *,
    known_tools: Collection[str] | None = None,
) -> ScaffoldingFold:
    """Remove narrated tool-call blocks from *text* (see the module docstring).

    *known_tools* is the run's tool vocabulary (:func:`transcript_tool_names`):
    a block whose name is not in it is folded only when it carries an explicit
    ``Tool Call Failed`` line.  ``None`` disables the check.
    """
    if not text or ("\n" not in text and not _FAILED_LINE_RE.match(text)):
        return ScaffoldingFold(text, 0, ())
    lines = text.split("\n")
    scanner = _Scanner(lines)
    spans: list[tuple[int, int]] = []
    names: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        match = scanner.match_block(i)
        if match is not None:
            end, name, failed = match
            if known_tools is None or failed or name in known_tools:
                spans.append((i, end))
                if name not in names:
                    names.append(name)
                i = end
                continue
            # A well-formed block naming a tool this run never touched and
            # reporting no failure: an API write-up, not a transcript.
            i += 1
            continue
        if _FAILED_LINE_RE.match(lines[i]):
            end = scanner.wrapped_line_end(i)
            logical = " ".join(lines[i:end])
            if _FAILED_MARKER_RE.search(logical):
                spans.append((i, end))
                failed_name = _FAILED_TOOL_RE.search(logical)
                if failed_name and failed_name.group("name") not in names:
                    names.append(failed_name.group("name"))
                i = end
                continue
        i += 1
    if not spans:
        return ScaffoldingFold(text, 0, ())
    removed = {idx for start, end in spans for idx in range(start, end)}
    kept = "\n".join(line for idx, line in enumerate(lines) if idx not in removed)
    cleaned = _MANY_BLANK_LINES_RE.sub(r"\1\1", kept).strip()
    return ScaffoldingFold(cleaned, len(spans), tuple(names))


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def _payload_tail_is_clean(tail: str) -> bool:
    """True when nothing but markup / punctuation follows a JSON payload."""
    return not tail.strip(_PAYLOAD_TAIL_CHARS)


class _Scanner:
    """The grammar over one answer's lines, with the scan budget it may spend."""

    __slots__ = ("budget", "lines", "n")

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.n = len(lines)
        self.budget = _MAX_SCAN_LINES_TOTAL

    def skip_blank(self, i: int) -> int:
        while i < self.n and not self.lines[i].strip():
            i += 1
        return i

    def wrapped_line_end(self, i: int) -> int:
        """One logical line: the model rarely breaks a fabricated failure
        line, so a continuation is only taken after a trailing comma (a
        wrapped tool listing).  Prose on the next line stays."""
        lines = self.lines
        end = i + 1
        while end < self.n and lines[end].strip() and lines[end - 1].rstrip().endswith(","):
            end += 1
        return end

    def object_end(self, line_idx: int, col: int) -> tuple[int, int] | None:
        """``(line, column)`` just past the ``}`` closing the object opened at
        ``lines[line_idx][col]``.

        Brace counting with string awareness; a string is closed at the end
        of its line (JSON strings never span lines), so an unbalanced quote in
        a malformed argument cannot swallow the rest of the answer.  ``None``
        when the object does not close within ``_MAX_PAYLOAD_LINES`` lines or
        the fold's total budget - the caller then treats the candidate as
        ordinary text.
        """
        depth = 0
        last = min(self.n, line_idx + _MAX_PAYLOAD_LINES)
        for idx in range(line_idx, last):
            if self.budget <= 0:
                return None
            self.budget -= 1
            in_string = False
            escaped = False
            line = self.lines[idx]
            for pos in range(col if idx == line_idx else 0, len(line)):
                ch = line[pos]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                elif ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return idx, pos + 1
        return None

    def json_payload(self, i: int, *, fenced: bool = False) -> int | None:
        """From ``lines[i]`` (blank lines allowed first): an optional code
        fence, a JSON object ending its line, the matching closing fence.
        Returns the index after it, or ``None`` when there is no such object.
        *fenced* is set when the opening fence sat on the label line."""
        lines = self.lines
        j = self.skip_blank(i)
        if not fenced and j < self.n and _FENCE_RE.match(lines[j]):
            fenced = True
            j = self.skip_blank(j + 1)
        if j >= self.n or not lines[j].lstrip().startswith("{"):
            return None
        end = self.object_end(j, lines[j].index("{"))
        if end is None:
            return None
        end_idx, end_col = end
        if not _payload_tail_is_clean(lines[end_idx][end_col:]):
            return None
        j = end_idx + 1
        if fenced:
            k = self.skip_blank(j)
            if k < self.n and _FENCE_RE.match(lines[k]):
                j = k + 1
        return j

    def inline_or_next_json(self, i: int, rest: str) -> int | None:
        """A label line whose JSON is inline (``Arguments: {...}``), opens a
        fence (``Arguments: ```json``), or follows on the next lines."""
        if rest.startswith("{"):
            end = self.object_end(i, self.lines[i].index("{"))
            if end is None:
                return None
            end_idx, end_col = end
            # ``Request: {"id": "x"} - I could not update it``: a sentence
            # with a payload in it, not a transcript block.
            if not _payload_tail_is_clean(self.lines[end_idx][end_col:]):
                return None
            return end_idx + 1
        if rest.startswith("```"):
            return self.json_payload(i + 1, fenced=True)
        if rest:
            return None
        return self.json_payload(i + 1)

    def match_block(self, i: int) -> tuple[int, str, bool] | None:
        """A narrated tool call starting at ``lines[i]``:
        ``(end, tool_name, has_failure_line)`` or ``None``."""
        lines = self.lines
        if len(lines[i]) > _MAX_NAME_LINE_CHARS:
            return None
        name_match = _NAME_LINE_RE.match(lines[i])
        if name_match is None:
            return None
        j = self.skip_blank(i + 1)
        if j >= self.n:
            return None
        request = _REQUEST_LINE_RE.match(lines[j])
        if request is None:
            return None
        after_request = self.inline_or_next_json(j, request.group("rest").strip())
        if after_request is None:
            return None
        j = after_request
        failed = False
        # Trailing sections: failure lines, and response-family labels whose
        # payload is a JSON object.  A label followed by prose keeps the prose.
        while True:
            k = self.skip_blank(j)
            if k >= self.n:
                break
            if _FAILED_LINE_RE.match(lines[k]):
                j = self.wrapped_line_end(k)
                failed = True
                continue
            response = _RESPONSE_LINE_RE.match(lines[k])
            if response is None:
                break
            rest = response.group("rest").strip()
            if rest.startswith("{") or rest.startswith("```") or not rest:
                after_json = self.inline_or_next_json(k, rest)
                if after_json is not None:
                    j = after_json
                    continue
                if not rest:
                    # Bare label, prose behind it: fold the label, keep the prose.
                    j = k + 1
                    break
            if _FAILED_LINE_RE.match(rest):
                # ``Result: Tool Call Failed: ...`` on one line.
                j = self.wrapped_line_end(k)
                failed = True
                continue
            break
        return j, name_match.group("name"), failed
