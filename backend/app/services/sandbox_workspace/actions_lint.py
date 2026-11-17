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

"""The ``actions-lint`` check: the shell of GitHub Actions workflows and composite actions, checked offline.

A change to ``.github/workflows/*.yml`` or to a composite action
(``action.yml``) is mostly shell inside ``run:`` blocks, which no manifest
check reads. This module is run by the detected check as a script
(``python -c <this file> [files...]``, :data:`detect.ACTIONS_LINT_SCRIPT`) in
the checkout's root, so it needs the standard library only; PyYAML, which the
toolchain images carry, adds the parse and schema findings when importable. It

1. parses every file as YAML (PyYAML), a syntax error reported at its line,
   and checks the shape GitHub reads: a workflow is a mapping with ``on`` and
   a ``jobs`` mapping; a composite action's ``runs.steps`` is a list whose
   every step has exactly one of ``run`` and ``uses``, and ``shell`` beside a
   ``run`` (actionlint does not read action files);
2. runs ``actionlint`` (which calls ``shellcheck`` when present) on the
   workflow files when the image has it on ``PATH``;
3. extracts every ``run:`` block whose step runs on bash or sh (the default
   when no ``shell:`` is given), replaces each ``${{ ... }}`` expression with a
   placeholder word, and runs ``bash -n`` on it, so a syntax error is reported
   at its line in the YAML file.

A layer the image cannot run (PyYAML not importable, ``actionlint`` not on
``PATH`` while a workflow is linted) is itself a finding, rule
``missing-layer``, so a green result always means every layer ran; the
argument ``--allow-missing-layers`` turns that off (a local run on a host
without them).

Findings are printed one per line as ``path:line: message`` and written as
SARIF to ``.xtm/actions-lint.sarif`` (the check's report). Exit status 1 when
there is a finding, 0 when every block parses. The YAML reading is
line-based on purpose: ``run:`` as a block scalar (``|``, ``>``, with chomping
indicators) or as a plain or quoted one-line value, inside a step mapping
(``- run:`` or a ``run:`` key below ``- name:``); a step's ``shell:`` naming
anything but bash or sh (pwsh, python, cmd) leaves it out.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

REPORT_PATH = ".xtm/actions-lint.sarif"
#: Files read at most, and bytes per file.
MAX_FILES = 400
MAX_FILE_BYTES = 1024 * 1024
#: Seconds one ``bash -n`` or the ``actionlint`` call may take.
BASH_TIMEOUT = 20
ACTIONLINT_TIMEOUT = 300
#: The repository's actionlint configuration, read in this order.
ACTIONLINT_CONFIGS = (".github/actionlint.yaml", ".github/actionlint.yml")

_KEY = re.compile(r"^(?P<indent>\s*)(?P<dash>-\s+)?(?P<key>[A-Za-z_][\w-]*)\s*:(?:\s+(?P<value>.*?))?\s*$")
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
_BLOCK = re.compile(r"^[|>][+-]?\d?[+-]?(\s+#.*)?$")
_BASH_LINE = re.compile(r"line (\d+): (.*)$")
_ACTIONLINT = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+): (?P<message>.*?)(?: \[(?P<rule>[\w-]+)\])?$")


def is_actions_file(path: str) -> bool:
    """A workflow (``.github/workflows/*.yml``) or a composite action file (``action.yml`` anywhere)."""
    name = path.rsplit("/", 1)[-1]
    if not name.endswith((".yml", ".yaml")):
        return False
    if path.startswith(".github/workflows/") and path.count("/") == 2:
        return True
    return name in ("action.yml", "action.yaml")


def _child(node, key: str):
    """The value node under *key* of a PyYAML mapping node, else ``None``."""
    if getattr(node, "tag", "").endswith(":map"):
        for name, value in node.value:
            if getattr(name, "value", None) == key:
                return value
    return None


def _finding(path: str, node, message: str) -> dict:
    line = node.start_mark.line + 1 if node is not None and getattr(node, "start_mark", None) else 1
    return {"path": path, "line": line, "rule": "actions-schema", "message": message}


def yaml_findings(path: str, text: str) -> list[dict]:
    """YAML syntax errors and the workflow or composite action shape of *text*; nothing without PyYAML."""
    try:
        import yaml  # optional: the script runs on images without it
    except ImportError:
        return []
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        message = " ".join(str(part) for part in (getattr(exc, "problem", None), getattr(exc, "context", None)) if part)
        return [
            {
                "path": path,
                "line": mark.line + 1 if mark else 1,
                "rule": "yaml-syntax",
                "message": message or str(exc).splitlines()[0],
            }
        ]
    if root is None or not root.tag.endswith(":map"):
        return [_finding(path, root, "the file is not a YAML mapping")]
    if path.rsplit("/", 1)[-1] in ("action.yml", "action.yaml"):
        return _action_findings(path, root)
    findings = []
    if _child(root, "on") is None and _child(root, "true") is None:
        findings.append(_finding(path, root, "a workflow needs an 'on' key"))
    jobs = _child(root, "jobs")
    if jobs is None or not jobs.tag.endswith(":map"):
        findings.append(_finding(path, jobs or root, "a workflow needs a 'jobs' mapping"))
    return findings


def _action_findings(path: str, root) -> list[dict]:
    runs = _child(root, "runs")
    if runs is None or not runs.tag.endswith(":map") or _child(runs, "using") is None:
        return [_finding(path, runs or root, "an action needs 'runs.using'")]
    if getattr(_child(runs, "using"), "value", "") != "composite":
        return []
    steps = _child(runs, "steps")
    if steps is None or not steps.tag.endswith(":seq"):
        return [_finding(path, steps or runs, "a composite action needs a 'runs.steps' list")]
    findings = []
    for step in steps.value:
        if not step.tag.endswith(":map"):
            findings.append(_finding(path, step, "a step is a mapping"))
            continue
        run, uses = _child(step, "run"), _child(step, "uses")
        if (run is None) == (uses is None):
            findings.append(_finding(path, step, "a step has exactly one of 'run' and 'uses'"))
        elif run is not None and _child(step, "shell") is None:
            findings.append(_finding(path, step, "a composite 'run' step needs 'shell'"))
    return findings


def _strip_comment(value: str) -> str:
    if value.startswith(("'", '"')):
        return value
    return re.sub(r"\s+#.*$", "", value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def run_blocks(text: str) -> list[tuple[int, str]]:
    """``(first line of the script in the file, the script)`` for every bash or sh ``run:`` of a YAML file."""
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        match = _KEY.match(lines[index])
        if not match or match.group("key") != "run":
            index += 1
            continue
        column = len(match.group("indent")) + len(match.group("dash") or "")
        value = _strip_comment(match.group("value") or "")
        if not value:
            index += 1
            continue  # `run:` with no value: a mapping (defaults.run), not a script
        if _BLOCK.match(value):
            body, first, cursor, block_indent = [], index + 2, index + 1, 0
            while cursor < len(lines):
                line = lines[cursor]
                if line.strip():
                    if _indent(line) <= column:
                        break
                    block_indent = block_indent or _indent(line)
                body.append(line[block_indent:] if line.strip() else "")
                cursor += 1
            while body and not body[-1]:
                body.pop()
            script = "\n".join(body)
            if value.startswith(">"):
                script = re.sub(r"(?<=\S)\n(?=\S)", " ", script)
            end = cursor
        else:
            first, script, end = index + 1, _unquote(value), index + 1
        if _runs_on_bash(lines, index, column):
            blocks.append((first, script))
        index = end
    return blocks


def _runs_on_bash(lines: list[str], at: int, column: int) -> bool:
    """Whether the step holding the ``run:`` key at line *at* runs on bash or sh (no ``shell:`` = bash)."""
    start = at
    while start > 0:
        match = _KEY.match(lines[start])
        if match and len(match.group("indent")) + len(match.group("dash") or "") == column and match.group("dash"):
            break
        previous = lines[start - 1]
        if previous.strip() and _indent(previous) < column and not previous.lstrip().startswith("-"):
            break
        start -= 1
    cursor = start
    while cursor < len(lines):
        line = lines[cursor]
        if cursor > start and line.strip():
            if _indent(line) < column:
                break
            match = _KEY.match(line)
            if match and match.group("dash") and len(match.group("indent")) + len(match.group("dash")) == column:
                break
        match = _KEY.match(line)
        if (
            match
            and match.group("key") == "shell"
            and len(match.group("indent")) + len(match.group("dash") or "") == column
        ):
            shell = _unquote(_strip_comment(match.group("value") or "")).split()
            return bool(shell) and shell[0].rsplit("/", 1)[-1] in ("bash", "sh")
        cursor += 1
    return True


def bash_findings(path: str, text: str) -> list[dict]:
    """``bash -n`` on every bash ``run:`` block of *text*, each error at its line in *path*."""
    findings = []
    for first, script in run_blocks(text):
        source = _EXPRESSION.sub("XTM_EXPRESSION", script)
        try:
            done = subprocess.run(["bash", "-n"], input=source, capture_output=True, text=True, timeout=BASH_TIMEOUT)
        except subprocess.TimeoutExpired:
            findings.append({"path": path, "line": first, "rule": "bash-n", "message": "bash -n timed out"})
            continue
        if done.returncode == 0:
            continue
        for raw in done.stderr.splitlines():
            match = _BASH_LINE.search(raw)
            if match:
                findings.append(
                    {"path": path, "line": first + int(match.group(1)) - 1, "rule": "bash-n", "message": match.group(2)}
                )
        if not any(f["path"] == path and f["line"] >= first for f in findings):
            findings.append(
                {
                    "path": path,
                    "line": first,
                    "rule": "bash-n",
                    "message": done.stderr.strip()[:400] or "bash -n failed",
                }
            )
    return findings


def actionlint_config(root: str = ".") -> str | None:
    """The repository's actionlint configuration file under *root*, if any.

    actionlint finds ``.github/actionlint.yaml`` only from a git project root;
    a sandbox checkout has no ``.git`` directory, so the file is passed explicitly.
    """
    for name in ACTIONLINT_CONFIGS:
        if os.path.isfile(os.path.join(root, name)):
            return name
    return None


def actionlint_findings(workflows: list[str], root: str = ".") -> list[dict]:
    """``actionlint`` on the workflow files when it is on ``PATH``, else nothing."""
    binary = shutil.which("actionlint")
    if not binary or not workflows:
        return []
    config = actionlint_config(root)
    argv = [binary, "-no-color", "-oneline", *(["-config-file", config] if config else []), *workflows]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=ACTIONLINT_TIMEOUT, cwd=root)
    except subprocess.TimeoutExpired:
        return [{"path": workflows[0], "line": 1, "rule": "actionlint", "message": "actionlint timed out"}]
    findings = []
    for raw in done.stdout.splitlines():
        match = _ACTIONLINT.match(raw.strip())
        if match:
            findings.append(
                {
                    "path": match.group("path"),
                    "line": int(match.group("line")),
                    "rule": f"actionlint/{match.group('rule')}" if match.group("rule") else "actionlint",
                    "message": match.group("message"),
                }
            )
    return findings


def discover(root: str = ".") -> list[str]:
    """The workflow and composite action files under *root*, relative to it, sorted."""
    found = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in ("node_modules", ".git", ".xtm", ".venv", "vendor"))
        for name in files:
            path = os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")
            if is_actions_file(path):
                found.append(path)
    return sorted(found)[:MAX_FILES]


def sarif(findings: list[dict]) -> dict:
    results = [
        {
            "ruleId": f["rule"],
            "level": "error",
            "message": {"text": f["message"]},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["path"]},
                        "region": {"startLine": max(f["line"], 1)},
                    }
                }
            ],
        }
        for f in findings
    ]
    return {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "actions-lint"}}, "results": results}]}


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401 - only to know whether the parse layer runs
    except ImportError:
        return False
    return True


def missing_layer_findings(files: list[str], workflows: list[str]) -> list[dict]:
    """One finding per layer the image cannot run: a silent skip would read as a clean result."""
    findings = []
    if files and not _has_yaml():
        message = "PyYAML is not in this image: the YAML parse and shape layer did not run"
        findings.append({"path": files[0], "line": 1, "rule": "missing-layer", "message": message})
    if workflows and not shutil.which("actionlint"):
        message = "actionlint is not in this image: the workflows were not linted"
        findings.append({"path": workflows[0], "line": 1, "rule": "missing-layer", "message": message})
    return findings


def main(argv: list[str]) -> int:
    allow_missing = "--allow-missing-layers" in argv
    files = [a for a in argv if not a.startswith("-")] or discover()
    files = [f for f in files if is_actions_file(f) and os.path.isfile(f)]
    findings: list[dict] = []
    blocks = 0
    for path in files:
        with open(path, "rb") as handle:
            text = handle.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")
        blocks += len(run_blocks(text))
        findings.extend(yaml_findings(path, text))
        findings.extend(bash_findings(path, text))
    workflows = [f for f in files if f.startswith(".github/workflows/")]
    findings.extend(actionlint_findings(workflows))
    if not allow_missing:
        findings.extend(missing_layer_findings(files, workflows))
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(sarif(findings), handle)
    for f in findings:
        print(f"{f['path']}:{f['line']}: {f['message']} [{f['rule']}]")
    parsed = "YAML parse and shape, " if _has_yaml() else "(no YAML parse: PyYAML is not in this image) "
    linter = "actionlint and bash -n" if shutil.which("actionlint") else "bash -n (actionlint is not in this image)"
    tool = parsed + linter
    print(f"actions-lint: {len(files)} file(s), {blocks} bash run block(s), {len(findings)} finding(s), {tool}.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
