# SPDX-License-Identifier: Apache-2.0

"""Line guards shared by the Markdown renderers of outgoing messages (Teams, email).

Python-Markdown reads a few lines differently from CommonMark, the dialect
agents write in. These guards rewrite one line of agent Markdown (outside
fenced code, which the callers skip) so it renders the way the author meant.
"""

from __future__ import annotations

import re

#: A line that opens with ``#`` glued to its text (``#9195 is the refill PR``),
#: after an optional indent, list marker or blockquote marker. CommonMark needs
#: a space after the ``#`` run for a heading; Python-Markdown does not, so the
#: line became a big ``<h1>``. ``# Title`` and ``## Title`` do not match.
_GLUED_HASH_RE = re.compile(r"^(?P<lead>[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+|>[ \t]?)*)(?P<hashes>#+)(?=[^#\s])")


def escape_glued_hash(line: str) -> str:
    """*line* with a line-start ``#`` glued to its text escaped (``\\#9195``).

    The escape renders as a plain ``#`` in a paragraph. A real ATX heading
    (``# Title``), a lone ``#`` and any other line are returned unchanged.
    """
    return _GLUED_HASH_RE.sub(lambda m: f"{m.group('lead')}\\{m.group('hashes')}", line, count=1)


def _pipe_row_cells(line: str) -> int:
    """The cell count of a pipe row (``| a | b |``), 0 when *line* is not one."""
    s = line.strip()
    if len(s) < 2 or not (s.startswith("|") and s.endswith("|")):
        return 0
    return len(s[1:-1].replace(r"\|", "").split("|"))


def _is_delimiter(line: str) -> bool:
    s = line.strip().strip("|")
    cells = [c.strip() for c in s.split("|")]
    return bool(cells) and all(re.fullmatch(r":?-+:?", c) for c in cells)


def add_missing_table_delimiters(lines: list[str], in_code: list[bool]) -> tuple[list[str], list[bool]]:
    """Give a pipe table written without its separator row (``| --- |``) one.

    Two or more consecutive lines outside fenced code that start and end with
    ``|`` and have the same cell count are a table; when the line under the
    first one is not a delimiter row, ``| --- | ... |`` is inserted there so
    the renderer sees a table instead of raw pipes. A single pipe line, a
    table that has its separator and pipes inside code are left unchanged.
    Returns the new lines and their code mask.
    """
    out: list[str] = []
    code: list[bool] = []
    previous_pipe = False
    for idx, line in enumerate(lines):
        out.append(line)
        code.append(in_code[idx])
        if in_code[idx]:
            previous_pipe = False
            continue
        cells = _pipe_row_cells(line)
        if (
            cells
            and not previous_pipe
            and not _is_delimiter(line)
            and idx + 1 < len(lines)
            and not in_code[idx + 1]
            and _pipe_row_cells(lines[idx + 1]) == cells
            and not _is_delimiter(lines[idx + 1])
        ):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + "| " + " | ".join(["---"] * cells) + " |")
            code.append(False)
        previous_pipe = bool(cells)
    return out, code
