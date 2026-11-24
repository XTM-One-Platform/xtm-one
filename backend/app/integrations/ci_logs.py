# SPDX-License-Identifier: Apache-2.0

"""A CI job log reduced to its failures: the summary a red check needs.

Pure: no I/O. A job log of tens of thousands of lines holds a few hundred
that explain the failure: the runner's own ``##[error]`` lines, the test
runner's failure report (pytest's ``FAILURES`` and ``short test summary info``
sections, a JUnit-style ``FAIL`` line, a linter's ``path:line:col: CODE``
finding, a compiler's ``error:``), each under the step that produced it. The
summary keeps those, bounded, and says what was left out, so a 40 KB tail is
read only when the summary is not enough.

Format-agnostic where it can be: the step markers are the ones the GitHub
Actions and Azure Pipelines runners write (``##[group]`` / ``##[endgroup]``,
``##[error]``); a log without them is one unnamed step.

The runner's echo of a step (``##[group]Run ...`` up to its ``##[endgroup]``:
the script, ``with:`` and ``env:``) is the command, not its output: it is
never scanned for a cue (``expect-error: false`` is no error). A log with no
cue left (a step that dies without an ``##[error]`` line, an upload cut) still
says where the job stopped: the last lines of the last step before the
post-job cleanup, encoded dumps omitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Characters a summary may hold.
MAX_SUMMARY_CHARS = 12_000
#: Characters one line of a summary may hold: a longer line keeps its start and end.
MAX_LINE_CHARS = 600
#: Budget below which a block that does not fit is counted as not shown rather than clipped.
MIN_CLIP_BUDGET = 400
#: Lines kept after a failure cue when no delimited section follows.
TRAILING_LINES = 12
#: Lines a delimited section (pytest FAILURES) may contribute.
MAX_SECTION_LINES = 250
#: Lines kept BEFORE the runner's bare "Process completed with exit code N": the
#: tool's own explanation (a docker pull error, a vitest or Playwright report)
#: is written above it and often matches no cue of its own.
LEADING_LINES = 20
#: Lines of the step the job stopped in, shown when no failure cue is found.
STOPPED_LINES = 40
#: Characters those lines may hold.
MAX_STOPPED_CHARS = 6_000
#: An unbroken run of encoded characters this long (a base64 workflow file in an env dump) is omitted.
MIN_ENCODED_CHARS = 200
#: Characters of one raw log line that are scanned: a longer line keeps its start and end only.
#: A pytest id carrying a 5 MB parametrised bytes value printed two 20 MB lines; scanning
#: them with the cue patterns held the event loop for seconds and, read again on every
#: resume, cut the turn that asked for the log each time (#9074).
MAX_SCAN_LINE_CHARS = 4_000
#: Finding lines (``path:line[:col]`` and an error or rule) a summary may hold, shown before every block.
MAX_FINDINGS = 150
#: Characters those lines may hold.
MAX_FINDINGS_CHARS = 6_000

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_GROUP = re.compile(r"^##\[group\](.*)$")
_ENDGROUP = re.compile(r"^##\[endgroup\]")
_RUNNER_ERROR = re.compile(r"^##\[error\]")
_EXIT_CODE_ERROR = re.compile(r"^##\[error\]Process completed with exit code [1-9]")
# A pytest section header: "===== FAILURES =====", "===== short test summary info =====".
_SECTION = re.compile(r"^=+ (FAILURES|ERRORS|short test summary info) =+$")
_SECTION_END = re.compile(r"^=+ .* =+$")
# A cue a line is about a failure, whatever the runner.
_CUE = re.compile(
    r"(^FAILED |^FAIL[: ]|^ERROR[: ]|\berror\[?[A-Z]*\d*\]?:|\berror:|Traceback \(most recent call last\)|"
    r"^E\s{2,}|\bAssertionError\b|^\S+:\d+:\d+: [A-Z]+\d{3,4}\b|Process completed with exit code [1-9]|"
    r"\bnpm ERR!|✖ \d+ problems?|Type error:|error TS\d+)",
)
# A test runner's progress line about a test that did not fail: never worth a line of the summary.
# pytest -v prints "path::test PASSED"; with xdist "[gw1] [ 31%] PASSED path::test" after a bare
# "path::test" line. A test id's parameters often carry "error:" or "401", which are no failure.
_PROGRESS_OUTCOME = re.compile(
    r"^(\[gw\d+\]\s+)?\[\s*\d+%\]\s+(PASSED|SKIPPED|XFAIL|XPASS)\b"
    r"|::\S.*\s(PASSED|SKIPPED|XFAIL|XPASS)(\s+\[\s*\d+%\])?\s*$"
)
_TEST_ID_LINE = re.compile(r"^[\w./-]+\.py::\S")
_FAILED_OUTCOME = re.compile(r"\b(FAILED|ERROR)\b")
# The runner's own group opening a step: its lines up to the endgroup are the step's echo.
_STEP_GROUP = re.compile(r"^Run ")
# Where a job's post-job cleanup starts: the lines after it are no step's output.
_POST_JOB = re.compile(r"^(Post job cleanup\.|Cleaning up orphan processes)")
_ENCODED = re.compile(rf"[A-Za-z0-9+/=_-]{{{MIN_ENCODED_CHARS},}}")
# A linter's, compiler's or test runner's finding: a file, a line and what is wrong, one line each.
_FINDING = re.compile(
    r"^\s*\S+?\(\d+,\d+\): error TS\d+"  # tsc
    r"|^\s*\S+?:\d+:\d+ - error TS\d+"  # tsc --pretty
    r"|^\s*\S+?: line \d+, col \d+, (?:Error|Warning) - "  # eslint compact
    r"|^\s*\S+?:\d+:\d+: [A-Z]+\d{1,4}\b"  # ruff concise, flake8
    r"|^[A-Z]{1,4}\d{3,4} (?:\[\*\] )?\S"  # ruff full: the rule line above its --> location
    r"|^\s*\S+?\.pyi?:\d+(?::\d+)?: error:"  # mypy
    r"|^\s*\S+?:\d+:\d+:? .*\[(?:Error|Warning)/\S+\]"  # oxlint unix
    r"|^\s*[×✗] \S"  # oxlint, vitest failed test
    r"|^(?:FAILED|ERROR) \S+::\S"  # pytest short summary
    r"|^[\w./-]+\.py:\d+: (?:\w*(?:Error|Exception)\b|Failed\b)"  # pytest location of the raise
    r"|^\s*FAIL\s+\S+\.\w+"  # vitest, jest failed file
    r"|^\s*● (?!Console\b)\S"  # jest failed test, never its console capture
)
# A location line under a finding: ruff's "-->", oxlint's "╭─[", vitest's "❯".
_LOCATION = re.compile(r"^\s*(?:-->|╭─\[|,-\[|❯)\s*\S+:\d+:\d+")
# eslint stylish: a file path alone on its line, then one indented "line:col  error  message  rule" per finding.
_STYLISH_FILE = re.compile(r"^(?:[A-Za-z]:)?[/\\]?(?:[\w.@-]+[/\\])+[\w.@-]+\.\w+$")
_STYLISH_ENTRY = re.compile(r"^\s+\d+:\d+\s+(error|warning)\s+\S")
# A row of a rule timing table (eslint TIMING): the first lines dropped when a summary runs out of room.
_TIMING_ROW = re.compile(
    r"^(?:Rule\s+\|\s*Time \(ms\)\s*\|\s*Relative|:?-{3,}\|[-:|]+|\S+\s*\|\s*[\d.]+\s*\|\s*[\d.]+%)\s*$"
)


def _is_progress(line: str) -> bool:
    """Whether *line* reports a test that did not fail (or only names a test about to run)."""
    if _PROGRESS_OUTCOME.search(line):
        return True
    return bool(_TEST_ID_LINE.match(line)) and not _FAILED_OUTCOME.search(line)


def _is_finding(line: str) -> bool:
    return bool(_FINDING.search(line))


#: Non-blank lines a location line (``--> path:line:col``) may sit under its finding: alone it is a
#: bundler warning's frame (rolldown's ``╭─[ node_modules/...:1:1 ]``), no finding.
LOCATION_REACH = 3


def _findings(lines: list[str]) -> list[str]:
    """The finding lines of one step, in log order, each once.

    An eslint stylish entry keeps its file line above it. Warning entries are
    kept only when the step has no error finding (a ``--max-warnings 0`` run).
    """
    found: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    path: str | None = None
    shown: dict[str, bool] = {"error": False, "warning": False}
    since = LOCATION_REACH + 1  # non-blank lines since the last finding
    for line in lines:
        if line.strip():
            since += 1
        if _LOCATION.match(line):
            if since <= LOCATION_REACH + 1 and line not in seen:
                seen.add(line)
                found.append(line)
            continue
        if _STYLISH_FILE.match(line):
            path, shown = line, {"error": False, "warning": False}
            continue
        entry = _STYLISH_ENTRY.match(line)
        if entry and path is not None:
            target = found if entry.group(1) == "error" else warnings
            if not shown[entry.group(1)]:
                target.append(path)
                shown[entry.group(1)] = True
            target.append(line)
            continue
        path = None
        if line.strip() and not _is_progress(line) and _is_finding(line):
            since = 1
            if line not in seen:
                seen.add(line)
                found.append(line)
    return found or warnings


def _without_findings(block: list[str], findings: set[str]) -> list[str]:
    """*block* without the lines the findings section shows (a pytest section is kept whole); [] when nothing is left."""
    if _SECTION.match(block[0]):
        return block
    if _EXIT_CODE_ERROR.match(block[0]):
        before = [ln for ln in block[2:] if ln not in findings]
        if not before:
            return block[:1]
        return [block[0], f"(the step's last {len(before)} line(s) before it, the findings above left out:)", *before]
    kept = [ln for ln in block if ln not in findings]
    return kept if any(ln.strip() for ln in kept) else []


def _without_timing(block: list[str]) -> list[str]:
    """*block* without its timing table rows, their count named."""
    kept = [ln for ln in block if not _TIMING_ROW.match(ln)]
    if len(kept) == len(block):
        return block
    return [*kept, f"[{len(block) - len(kept)} timing table line(s) dropped to fit]"]


def _rank(block: list[str]) -> int:
    """The order a block is shown in: what names the failures first, the cue lines last.

    A capped summary must never drop the test runner's own report for the
    lines around a cue: the short summary (one line per failure), then the
    FAILURES / ERRORS sections, then the runner's ``##[error]`` lines, then
    every other cue.
    """
    head = block[0] if block else ""
    section = _SECTION.match(head)
    if section:
        return 0 if section.group(1) == "short test summary info" else 1
    if _RUNNER_ERROR.match(head):
        return 2
    return 3


@dataclass
class Step:
    name: str
    lines: list[str] = field(default_factory=list)
    #: The runner's echo of the step (script, ``with:``, ``env:``): never scanned for a cue.
    header: list[str] = field(default_factory=list)


@dataclass
class Summary:
    steps_failed: list[str] = field(default_factory=list)
    blocks: list[tuple[str, list[str]]] = field(default_factory=list)
    total_lines: int = 0
    kept_lines: int = 0
    #: The finding lines (file, line, rule) per step, in log order: shown before every block.
    findings: list[tuple[str, list[str]]] = field(default_factory=list)
    #: With no block: the step the job stopped in and its last lines before the post-job cleanup.
    stopped_step: str = ""
    stopped_lines: list[str] = field(default_factory=list)
    #: Failure blocks left out because an earlier block is the same but for numbers and addresses.
    repeated_blocks: int = 0


def _bounded(line: str) -> str:
    """*line* at most :data:`MAX_SCAN_LINE_CHARS` characters, its start and end kept, the cut named."""
    if len(line) <= MAX_SCAN_LINE_CHARS:
        return line
    keep = MAX_SCAN_LINE_CHARS // 2
    return f"{line[:keep]} [... {len(line) - 2 * keep:,} characters of this line not scanned ...] {line[-keep:]}"


def clean_line(line: str) -> str:
    """A raw log line bounded (:func:`_bounded`), its timestamp and colours stripped."""
    return _ANSI.sub("", _TIMESTAMP.sub("", _bounded(line.rstrip("\r")).lstrip("\ufeff")))


def split_steps(text: str) -> list[Step]:
    """The log as the runner's groups, timestamps and colours stripped.

    A ``Run ...`` group opens a step and its lines up to the ``##[endgroup]``
    are the step's header (a group never closed keeps them as output). A group
    of another name inside a step's output (an action's "Uploading results")
    stays in that step as a ``> name`` line; outside one it opens a step.
    """
    steps: list[Step] = [Step(name="")]
    echo: list[str] | None = None  # the open Run group's lines, until its endgroup
    in_run_step = False
    for raw in text.splitlines():
        line = clean_line(raw)
        group = _GROUP.match(line)
        if group:
            name = group.group(1).strip()
            if echo is not None:
                steps[-1].lines.extend(echo)
                echo = None
            if in_run_step and not _STEP_GROUP.match(name):
                steps[-1].lines.append(f"> {name}")
                continue
            steps.append(Step(name=name or f"step {len(steps)}"))
            in_run_step = bool(_STEP_GROUP.match(name))
            echo = [] if in_run_step else None
            continue
        if _ENDGROUP.match(line):
            if echo is not None:
                steps[-1].header = echo
                echo = None
            continue
        if echo is not None:
            echo.append(line)
        else:
            steps[-1].lines.append(line)
    if echo is not None:
        steps[-1].lines.extend(echo)
    return [s for s in steps if s.lines or s.name]


def _omit_encoded(line: str) -> str:
    return _ENCODED.sub(lambda m: f"[{len(m.group(0)):,}-character encoded value omitted]", line)


def _stopped_at(steps: list[Step]) -> tuple[str, list[str]]:
    """The last step with output before the post-job cleanup, and its last lines (bounded, in log order)."""
    for step in reversed(steps):
        lines = step.lines
        cut = next((k for k, ln in enumerate(lines) if _POST_JOB.match(ln)), len(lines))
        lines = [ln for ln in lines[:cut] if ln.strip()]
        if not lines:
            continue
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            if len(kept) >= STOPPED_LINES:
                break
            line = _clip_line(_omit_encoded(line))
            if used + len(line) + 1 > MAX_STOPPED_CHARS:
                break
            kept.append(line)
            used += len(line) + 1
        return step.name, list(reversed(kept))
    return "", []


def _failure_blocks(lines: list[str]) -> list[list[str]]:
    """The failure sections and cue lines (with their trailing lines) of one step."""
    blocks: list[list[str]] = []
    i, n = 0, len(lines)
    kept_until = 0  # lines before this index already belong to a block
    while i < n:
        line = lines[i]
        if _EXIT_CODE_ERROR.match(line):
            # The runner's last word on a failed step: keep what the step printed
            # just before it, never a line an earlier block already shows.
            start = max(kept_until, i - LEADING_LINES)
            before = [ln for ln in lines[start:i] if ln.strip() and not _is_progress(ln)]
            block = [line]
            if before:
                block.append(f"(the step's last {len(before)} line(s) before it:)")
                block.extend(before)
            blocks.append(block)
            i += 1
            kept_until = i
            continue
        if _SECTION.match(line):
            block = [line]
            i += 1
            while i < n and not (_SECTION_END.match(lines[i]) and not _SECTION.match(lines[i])) and len(block) < MAX_SECTION_LINES:
                if _SECTION.match(lines[i]):
                    break
                block.append(lines[i])
                i += 1
            if i < n and _SECTION_END.match(lines[i]) and not _SECTION.match(lines[i]):
                block.append(lines[i])  # the "N failed, M passed in Xs" line
                i += 1
            blocks.append(block)
            kept_until = i
            continue
        if _RUNNER_ERROR.match(line) or (_CUE.search(line) and not _is_progress(line)):
            block = [line]
            j = i + 1
            while j < n and j < i + 1 + TRAILING_LINES and not _SECTION.match(lines[j]):
                if _EXIT_CODE_ERROR.match(lines[j]):
                    break  # its own block, with what precedes it
                if not _is_progress(lines[j]):
                    block.append(lines[j])
                j += 1
            blocks.append(block)
            i = j
            kept_until = j
            continue
        i += 1
    return blocks


#: What makes two failure blocks the same failure: a memory address, then a standalone number
#: (a pid, a thread id, a line or frame number, a duration). A digit inside a name
#: (``test_shard_2``, ``worker3``) is kept, so two different tests never fold into one.
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_DIGITS = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
#: Non-blank lines a block needs before a repeat of it is folded: a one-line cue repeated
#: by several steps stays, each under its step.
MIN_FOLDED_LINES = 3


def _block_key(block: list[str]) -> tuple[str, ...]:
    """*block* with its addresses and numbers masked: equal keys are one failure printed again."""
    return tuple(_DIGITS.sub("#", _HEX_ADDRESS.sub("0x#", ln.strip())) for ln in block if ln.strip())


def _collapse_repeats(blocks: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, list[str]]], int]:
    """*blocks* with every block equal to an earlier one (:func:`_block_key`) dropped, the first one counting them.

    A setup error retried by a test runner, or one crash printed by every worker,
    writes the same block (a C stack of a hundred lines) once per attempt: the
    reader needs it once and the count, not seven copies filling the summary
    and squeezing the other failures out. Returns the blocks kept and the
    number dropped.
    """
    first: dict[tuple[str, ...], int] = {}
    repeats: list[int] = []
    kept: list[tuple[str, list[str]]] = []
    for step, block in blocks:
        # The test runner's report (pytest FAILURES, short test summary) is never folded.
        key = _block_key(block) if not _SECTION.match(block[0] if block else "") else ()
        if len(key) < MIN_FOLDED_LINES:
            key = ()
        if key and key in first:
            repeats[first[key]] += 1
            continue
        if key:
            first[key] = len(kept)
        kept.append((step, block))
        repeats.append(0)
    for k, count in enumerate(repeats):
        if count:
            step, block = kept[k]
            kept[k] = (
                step,
                [
                    *block,
                    f"[the same block, but for numbers and addresses, appears {count} more time(s) in the log: shown once]",
                ],
            )
    return kept, sum(repeats)


def summarize(text: str) -> Summary:
    out = Summary(total_lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0))
    steps = split_steps(text)
    collected: list[tuple[str, list[str]]] = []
    for step in steps:
        blocks = _failure_blocks(step.lines)
        # A line a pytest section already shows (its short summary, its raise locations) is not repeated.
        in_sections = {ln for block in blocks if _SECTION.match(block[0]) for ln in block}
        findings = [ln for ln in _findings(step.lines) if ln not in in_sections]
        if findings:
            out.findings.append((step.name, findings))
            out.kept_lines += len(findings)
            blocks = [b for b in (_without_findings(block, set(findings)) for block in blocks) if b]
        if not blocks and not findings:
            continue
        out.steps_failed.append(step.name or "(no step marker)")
        collected.extend((step.name, block) for block in blocks)
    out.blocks, out.repeated_blocks = _collapse_repeats(collected)
    out.kept_lines += sum(len(block) for _, block in out.blocks)
    if not out.blocks and not out.findings:
        out.stopped_step, out.stopped_lines = _stopped_at(steps)
    return out


#: Lines of the log's end a default job log read adds after the failure blocks.
LAST_LINES = 25
#: Characters those lines may hold.
MAX_LAST_CHARS = 3_000


def last_lines(text: str, *, count: int = LAST_LINES, max_chars: int = MAX_LAST_CHARS) -> list[str]:
    """The last *count* non-blank lines of the log, cleaned, at most *max_chars* characters, in log order."""
    kept: list[str] = []
    used = 0
    for raw in reversed(text.splitlines()):
        line = clean_line(raw)
        if not line.strip() or _GROUP.match(line) or _ENDGROUP.match(line):
            continue
        if len(kept) >= count:
            break
        if len(line) > max_chars // 4:
            # An over-long line is clipped to a quarter of
            # the budget, its end kept, and the scan goes on to earlier lines.
            line = "..." + line[-(max_chars // 4 - 3) :]
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    return list(reversed(kept))


_DIFF_GIT = re.compile(r"^diff --git ")
_OLD_FILE = re.compile(r"^--- (a/|/dev/null)")
_NEW_FILE = re.compile(r"^\+\+\+ (b/|/dev/null)")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
#: Header lines git writes between ``diff --git`` and ``---`` (modes, index, renames, binary).
_GIT_HEADER = re.compile(
    r"^(index |old mode |new mode |deleted file mode |new file mode |similarity index |dissimilarity index |"
    r"rename from |rename to |copy from |copy to |Binary files )"
)


def _patch_line(raw: str) -> str:
    """A raw log line as the diff printed it: the runner timestamp and colours removed, never clipped."""
    return _ANSI.sub("", _TIMESTAMP.sub("", raw.rstrip("\r").lstrip("\ufeff")))


def _hunk_end(lines: list[str], start: int) -> int | None:
    """The index after the hunk whose ``@@`` header is ``lines[start]``, or ``None`` when it is cut short.

    The header's counts say how many old and new lines follow; a blank line is a context line
    that lost its leading space in the log, a ``\\ No newline`` marker counts for neither side.
    """
    match = _HUNK.match(lines[start])
    if match is None:
        return None
    old = int(match.group(1)) if match.group(1) is not None else 1
    new = int(match.group(2)) if match.group(2) is not None else 1
    index = start + 1
    while old > 0 or new > 0:
        if index >= len(lines):
            return None
        line = lines[index]
        if line.startswith("\\"):
            index += 1
            continue
        tag = line[:1]
        if tag == "-":
            old -= 1
        elif tag == "+":
            new -= 1
        elif tag in (" ", ""):
            old -= 1
            new -= 1
        else:
            return None
        if old < 0 or new < 0:
            return None
        index += 1
    while index < len(lines) and lines[index].startswith("\\"):
        index += 1
    return index


def extract_patch(text: str) -> str | None:
    """The unified diff(s) a job log printed, as ``git apply`` and ``sandbox_apply_patch`` take them.

    A formatter or generator check (``ruff format --diff``, ``git diff --exit-code`` after
    codegen, prettier, spotless) prints the change it wants. Each file block starts at a
    ``diff --git`` line or a ``--- a/`` line directly followed by ``+++ b/``; its hunks are
    kept while their ``@@`` counts are met, so the runner's next line (``##[error]``, the
    tool's verdict) never joins the patch. A context line the log printed blank is written
    back with its leading space. ``None`` when no block has a complete hunk.
    """
    lines = [_patch_line(raw) for raw in text.splitlines()]
    out: list[str] = []
    index = 0
    while index < len(lines):
        header: list[str] = []
        cursor = index
        if _DIFF_GIT.match(lines[cursor]):
            header.append(lines[cursor])
            cursor += 1
            while cursor < len(lines) and _GIT_HEADER.match(lines[cursor]):
                header.append(lines[cursor])
                cursor += 1
        if not (cursor + 1 < len(lines) and _OLD_FILE.match(lines[cursor]) and _NEW_FILE.match(lines[cursor + 1])):
            index += 1
            continue
        header += [lines[cursor], lines[cursor + 1]]
        cursor += 2
        hunks: list[str] = []
        while cursor < len(lines) and _HUNK.match(lines[cursor]):
            end = _hunk_end(lines, cursor)
            if end is None:
                break
            hunks += [line if line else " " for line in lines[cursor:end]]
            cursor = end
        if hunks:
            out += header + hunks
            index = cursor
        else:
            index += 1
    if not out:
        return None
    return "\n".join(out) + "\n"


def patch_file_count(patch: str) -> int:
    """How many files a patch :func:`extract_patch` returned changes (its ``+++`` lines)."""
    return sum(1 for line in patch.splitlines() if _NEW_FILE.match(line))


def _clip_line(line: str) -> str:
    """*line* at most :data:`MAX_LINE_CHARS` characters, its start and end kept."""
    if len(line) <= MAX_LINE_CHARS:
        return line
    keep = (MAX_LINE_CHARS - 5) // 2
    return f"{line[:keep]} ... {line[-keep:]}"


def _fit_block(header: str, block: list[str], budget: int) -> str | None:
    """The block as summary text within *budget* characters, clipped to its first lines when too long.

    ``None`` when not even the header and one line fit. A clipped block ends
    with a marker naming the lines kept, so the reader knows the rest exists.
    """
    lines = [_clip_line(ln) for ln in block if ln.strip()]
    if not lines:
        return None
    chunk = "\n".join([header, *lines])
    if len(chunk) <= budget:
        return chunk
    if budget < MIN_CLIP_BUDGET:
        return None
    marker_room = 120
    used = len(header)
    kept: list[str] = []
    for line in lines:
        if used + len(line) + 1 > budget - marker_room:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return None
    marker = f"[block clipped: {len(kept)} of {len(lines)} line(s) shown; to_workspace_path gives the whole log]"
    return "\n".join([header, *kept, marker])


def job_log_answer(text: str, *, title: str, failures_only: bool) -> str | None:
    """The default read of a job log: the failure summary, then (unless *failures_only*) its last lines.

    ``None`` when the log holds no failure cue and *failures_only* is not set:
    the caller then shows the raw tail. CPU-bound on a large log, so the
    caller runs it in a thread (``asyncio.to_thread``), never on the event loop.
    """
    summary = summarize(text)
    if failures_only:
        return format_summary(summary, title=title, log_chars=len(text))
    if not summary.blocks and not summary.findings:
        return None
    out = format_summary(summary, title=title, log_chars=len(text))
    last = last_lines(text)
    if last:
        out += f"\n\n--- the log's last {len(last)} line(s) ---\n" + "\n".join(last)
    return out


#: Where the findings go in the order of the summary's blocks.
_FINDINGS_SLOT = "\x00findings"


def _format_findings(findings: list[tuple[str, list[str]]], total: int, *, max_chars: int) -> str:
    """The findings section: every finding line, at most :data:`MAX_FINDINGS` and *max_chars*, the cut counted."""
    out = [f"\n--- findings: {total:,} line(s) (file, line, rule) ---"]
    used = len(out[0])
    shown = 0
    for step, block in findings:
        if shown >= MAX_FINDINGS:
            break
        if len(findings) > 1:
            out.append(f"[step: {step or '(no step marker)'}]")
        for line in block:
            line = _clip_line(line)
            if shown >= MAX_FINDINGS or used + len(line) + 1 > max_chars - 120:
                break
            out.append(line)
            used += len(line) + 1
            shown += 1
    if shown < total:
        out.append(f"[{total - shown:,} more finding line(s) not shown; to_workspace_path gives the whole log]")
    return "\n".join(out)


def format_summary(summary: Summary, *, title: str, log_chars: int | None = None) -> str:
    whole = (
        f" The whole log ({log_chars:,} characters) is read with tail=true (its last 40,000 characters) or with "
        "to_workspace_path (all of it)."
        if log_chars
        else ""
    )
    if not summary.blocks and not summary.findings:
        head = (
            f"{title}: no failure cue found in {summary.total_lines:,} log lines (no ##[error], no test failure "
            "section, no linter finding)."
        )
        if not summary.stopped_lines:
            return f"{head} Read the tail with tail=true, or pass to_workspace_path."
        return "\n".join(
            [
                f"{head} The job stopped in step {summary.stopped_step or '(no step marker)'!r}; its last "
                f"{len(summary.stopped_lines)} line(s) before the post-job cleanup follow.{whole}",
                "",
                *summary.stopped_lines,
            ]
        )
    found = sum(len(f) for _, f in summary.findings)
    what = f"{found:,} finding line(s) and " if found else ""
    order = "the test runner's report first, then the findings" if found else "the test runner's report first"
    lines: list[str] = [
        f"{title}: {what}{len(summary.blocks)} failure block(s)"
        f"{f' ({summary.repeated_blocks} repeat(s) of them folded)' if summary.repeated_blocks else ''} in step(s) "
        f"{', '.join(repr(s) for s in summary.steps_failed)}; {summary.kept_lines:,} of {summary.total_lines:,} lines "
        f"kept, {order}.{whole}",
    ]
    budget = MAX_SUMMARY_CHARS - len(lines[0])
    current = None
    dropped = 0
    blocks = sorted(summary.blocks, key=lambda sb: _rank(sb[1]))  # stable: within a rank the log order holds
    report = [sb for sb in blocks if _rank(sb[1]) < 2]  # pytest's short summary and FAILURES / ERRORS
    others = blocks[len(report) :]
    findings = _format_findings(summary.findings, found, max_chars=min(MAX_FINDINGS_CHARS, budget)) if found else ""
    budget -= len(findings)  # reserved: the report's blocks never squeeze the findings out
    room = budget - sum(len(ln) + 1 for _, block in report for ln in block)
    if sum(len(ln) + 1 for _, block in others for ln in block) + 40 * len(others) > room:
        # Out of room: the timing tables go first, before any block is clipped or dropped.
        others = [(step, _without_timing(block)) for step, block in others]
    for step, block in [*report, ("", [_FINDINGS_SLOT]), *others]:
        if block == [_FINDINGS_SLOT]:
            if findings:
                # The findings come right after the test runner's report, ahead of every other block.
                lines.append(findings)
                current = None
            continue
        header = f"\n--- step: {step or '(no step marker)'} ---" if step != current else "\n..."
        chunk = _fit_block(header, block, budget)
        if chunk is None:
            dropped += 1
            continue
        budget -= len(chunk)
        lines.append(chunk)
        current = step
    if dropped:
        lines.append(f"\n[{dropped} more failure block(s) not shown: the summary is capped at {MAX_SUMMARY_CHARS:,} characters; pass to_workspace_path for the whole log]")
    return "\n".join(lines)
