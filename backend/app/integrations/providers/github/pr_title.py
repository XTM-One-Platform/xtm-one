"""The pull request and issue title rules a repository declares, checked before a write.

A repository whose CI fails a pull request on its title (a Conventional Commits
form, an issue suffix) says so where its sandbox checks live, in
``.xtm-one/checks.toml``::

    [pull_request]
    title_pattern = '^(feat|fix)(\\([a-z0-9-]+\\))?: [a-z0-9].* \\(#[0-9]+\\)$'
    title_rule = "type(scope?): description (#123)"
    title_example = "fix: handle missing auth token (#456)"

``create_github_pr`` and ``update_github_pr`` read that table on the target
branch and refuse a title that does not match before anything is written, so
the agent fixes the title at once instead of one CI round later. No file, no
table, or a file that cannot be read: nothing is checked and the write goes on
as before. A table that is malformed is named in the result, never a refusal.

An ``[issue]`` table with the same three keys declares the issue title rule
(the same form without the trailing reference, for instance), checked by
``create_github_issue`` and ``update_github_issue`` on the default branch.

A repository that declares no ``[pull_request]`` rule is read where its CI
checks the title: the workflows under ``.github/workflows`` whose file name
names a title or a convention. A step that runs a shell test of the pull
request title against a pattern variable (``[[ "$TITLE" =~ $PATTERN ]]`` or
``grep -P "$PATTERN"``) gives that pattern; a step that uses a shared
``pr-title-check`` action gives the action's rule (:data:`KNOWN_TITLE_ACTIONS`).
Steps that only run on a condition read from an earlier step (the body names
no public issue, for instance) are alternatives: a title that matches one of
them is written, with a note when it misses the first. No workflow found, or
one that cannot be read or parsed: the write goes on with "Title not checked".
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any

import yaml

from . import _common
from ._common import _BASE_URL, _PROVIDER

#: Where a repository declares its pull request title rule, next to its sandbox checks.
RULES_FILE = ".xtm-one/checks.toml"

#: The longest title pattern read: a rule is one line of a workflow, not a program.
MAX_PATTERN_CHARS = 1000

#: The longest title the pattern is matched against (GitHub caps a title at 256).
MAX_TITLE_CHARS = 256

#: The table of each rule and the object it names in a refusal.
PULL_REQUEST = "pull_request"
ISSUE = "issue"
_OBJECT = {PULL_REQUEST: "pull request", ISSUE: "issue"}


@dataclass(frozen=True)
class TitleRule:
    """A repository's title rule (pull request or issue): the pattern and how to say it."""

    pattern: re.Pattern[str]
    rule: str
    example: str
    #: Where the rule was read, named in a refusal (empty: the checks.toml table).
    source: str = ""
    #: A title holding this text is exempt (a dependency update, for instance).
    exempt: str = ""


def parse_title_rule(text: str, table_name: str = PULL_REQUEST) -> tuple[TitleRule | None, str | None]:
    """The ``[table_name]`` title rule of a ``checks.toml``: ``(rule, None)``,
    ``(None, None)`` when the file declares none, ``(None, why)`` when it is malformed."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, f"{RULES_FILE} is not valid TOML: {exc}"
    table = data.get(table_name)
    if table is None:
        return None, None
    if not isinstance(table, dict):
        return None, f"{RULES_FILE}: {table_name} must be a table."
    raw = table.get("title_pattern")
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip():
        return None, f"{RULES_FILE}: [{table_name}] title_pattern must be a non-empty string."
    if len(raw) > MAX_PATTERN_CHARS:
        return None, f"{RULES_FILE}: [{table_name}] title_pattern is at most {MAX_PATTERN_CHARS} characters."
    try:
        pattern = re.compile(raw)
    except re.error as exc:
        return None, f"{RULES_FILE}: [{table_name}] title_pattern is not a valid regular expression: {exc}"
    rule = table.get("title_rule")
    example = table.get("title_example")
    return (
        TitleRule(
            pattern=pattern,
            rule=rule.strip() if isinstance(rule, str) and rule.strip() else raw,
            example=example.strip() if isinstance(example, str) else "",
        ),
        None,
    )


def title_error(title: str, rule: TitleRule, repo: str, table_name: str = PULL_REQUEST) -> str | None:
    """The refusal for a *title* that breaks *rule*, ``None`` when it matches."""
    if rule.pattern.search(title[:MAX_TITLE_CHARS]) or (rule.exempt and rule.exempt in title):
        return None
    what = _OBJECT.get(table_name, table_name)
    source = rule.source or f"{RULES_FILE}, [{table_name}]"
    lines = [
        f"Error: the {what} title does not follow {repo}'s title rule ({source}); nothing was written.",
        f"  Title: {title}",
        f"  Rule: {rule.rule}",
    ]
    if rule.example:
        lines.append(f"  Example: {rule.example}")
    lines.append("Fix the title and call again.")
    return "\n".join(lines)


async def read_title_rule(
    client: Any, owner: str, name: str, ref: str, headers: dict[str, str], table_name: str = PULL_REQUEST
) -> tuple[TitleRule | None, str | None]:
    """The title rule *owner/name* declares on *ref* (the default branch when empty).

    ``(None, None)`` when the repository declares none or the file cannot be
    read: the write goes on unchecked, as before the rule existed.
    """
    params = {"ref": ref} if ref else None
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/contents/{RULES_FILE}",
        _PROVIDER,
        f"read the {_OBJECT.get(table_name, table_name)} title rule of {owner}/{name}",
        headers={**headers, "Accept": "application/vnd.github.raw+json"},
        params=params,
    )
    if err or resp is None:
        return None, None
    return parse_title_rule(resp.text, table_name)


#: Where a repository's CI workflows live.
WORKFLOWS_DIR = ".github/workflows"

#: A workflow file whose name holds one of these words may check the pull request title.
_WORKFLOW_NAME_HINT = re.compile(r"title|convention", re.IGNORECASE)

#: The most workflow files read for one check, and the largest one parsed.
MAX_WORKFLOW_FILES = 4
MAX_WORKFLOW_BYTES = 100_000

_TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"

#: Shared title-check actions and the rule they enforce, keyed by the action's
#: directory name (the ``uses:`` path ends with it, before ``@ref``).
KNOWN_TITLE_ACTIONS: dict[str, TitleRule] = {
    "pr-title-check": TitleRule(
        pattern=re.compile(rf"^({_TYPES})(\([a-zA-Z0-9_.-]+\))?!?: .+ \(#[0-9]+(, ?#[0-9]+)*\)$"),
        rule="type(scope?)!?: description (#123)",
        example="fix: handle missing auth token (#456)",
        exempt="(deps)",
    ),
}

# A shell variable set to a quoted string, alone on its line.
_ASSIGNMENT = re.compile(
    r"""^\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_]\w*)=(?:'([^']*)'|"((?:[^"\\]|\\.)*)")\s*(?:#.*)?$"""
)
# The pattern variable a title is tested against: [[ ... =~ $P ]] or grep -P "$P".
_PATTERN_USE = re.compile(r"""(?:=~|grep\s+-[A-Za-z]*P[A-Za-z]*)\s+"?\$\{?([A-Za-z_]\w*)\}?"?""")
_EXPANSION = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
_ECHO = re.compile(r"\becho\s+(.*)$")


def _shell_variables(script: str) -> dict[str, str]:
    """The quoted string variables *script* sets, expanded in order."""
    found: dict[str, str] = {}
    for line in script.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        var, single, double = match.groups()
        if single is not None:
            found[var] = single
            continue
        value = re.sub(r'\\(["\\$`])', r"\1", double or "")
        found[var] = _EXPANSION.sub(lambda m: found.get(m.group(1) or m.group(2), m.group(0)), value)
    return found


def _echoed_segments(script: str) -> list[str]:
    """The quoted texts the *script* echoes, the inner single-quoted parts first."""
    segments: list[str] = []
    for line in script.splitlines():
        match = _ECHO.search(line)
        if not match:
            continue
        for text in re.findall(r'"([^"]*)"', match.group(1)) or [match.group(1)]:
            segments.extend(s.strip() for s in re.findall(r"'([^']+)'", text))
            segments.append(re.sub(r"^(?:[A-Za-z ]+:)?\s*", "", text.strip()))
    return [s for s in segments if s]


def _run_step_rule(script: str, source: str) -> TitleRule | None:
    """The title rule a ``run:`` script enforces, ``None`` when it tests no pattern variable."""
    variables = _shell_variables(script)
    for use in _PATTERN_USE.finditer(script):
        raw = variables.get(use.group(1))
        if not raw or len(raw) > MAX_PATTERN_CHARS:
            continue
        try:
            pattern = re.compile(raw)
        except re.error:
            continue
        segments = _echoed_segments(script)
        rule = next((s for s in segments if "type(" in s), raw)
        example = next((s for s in segments if pattern.search(s)), "")
        return TitleRule(pattern=pattern, rule=rule, example=example, source=source)
    return None


def workflow_title_rules(text: str, path: str) -> list[tuple[TitleRule, str]]:
    """The pull request title checks of one workflow file, in file order, each with
    the step condition it runs under (empty: always). A file that does not parse: none."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return []
    rules: list[tuple[TitleRule, str]] = []
    for job_id, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            label = f"{path}, job {job_id}, step {step.get('name') or step.get('id') or '?'}"
            condition = str(step.get("if") or "")
            uses = str(step.get("uses") or "").split("@", 1)[0].rstrip("/")
            action = KNOWN_TITLE_ACTIONS.get(uses.rsplit("/", 1)[-1]) if uses else None
            if action is not None:
                rules.append((TitleRule(**{**action.__dict__, "source": f"{label}, {uses}"}), condition))
                continue
            script = step.get("run")
            if not isinstance(script, str) or "pull_request.title" not in f"{step.get('env')}{script}":
                continue
            rule = _run_step_rule(script, label)
            if rule is not None:
                rules.append((rule, condition))
    return rules


async def _read_workflow_rules(
    client: Any, owner: str, name: str, ref: str, headers: dict[str, str]
) -> list[tuple[TitleRule, str]] | None:
    """The title checks of the workflows of *owner/name* on *ref* whose file name
    names a title or a convention; ``None`` when the workflow folder cannot be listed."""
    params = {"ref": ref} if ref else None
    resp, err = await _common.safe_request(
        client,
        "get",
        f"{_BASE_URL}/repos/{owner}/{name}/contents/{WORKFLOWS_DIR}",
        _PROVIDER,
        f"list the workflows of {owner}/{name}",
        headers=headers,
        params=params,
    )
    if err or resp is None:
        return None
    listing = resp.json()
    files = [
        entry["path"]
        for entry in (listing if isinstance(listing, list) else [])
        if isinstance(entry, dict)
        and entry.get("type") == "file"
        and str(entry.get("name", "")).endswith((".yml", ".yaml"))
        and _WORKFLOW_NAME_HINT.search(str(entry.get("name", "")))
        and isinstance(entry.get("path"), str)
    ]
    rules: list[tuple[TitleRule, str]] = []
    for path in files[:MAX_WORKFLOW_FILES]:
        resp, err = await _common.safe_request(
            client,
            "get",
            f"{_BASE_URL}/repos/{owner}/{name}/contents/{path}",
            _PROVIDER,
            f"read the workflow {path} of {owner}/{name}",
            headers={**headers, "Accept": "application/vnd.github.raw+json"},
            params=params,
        )
        if err or resp is None or len(resp.text) > MAX_WORKFLOW_BYTES:
            continue
        rules.extend(workflow_title_rules(resp.text, path))
    return rules


def workflow_title_verdict(
    title: str, rules: list[tuple[TitleRule, str]] | None, repo: str
) -> tuple[str | None, str | None]:
    """``(refusal, note)`` for *title* against the workflow checks *rules*.

    Every check that runs on every pull request must pass. Checks that run on a
    condition of an earlier step are alternatives: matching one writes the
    title with a note naming it, matching none refuses with the first rule.
    ``None`` (no workflow folder read): unchecked and silent, as before.
    """
    if rules is None:
        return None, None
    if not rules:
        return None, f"Title not checked: {repo} declares no title rule and no title check was found in its workflows."
    always = [rule for rule, condition in rules if not condition]
    for rule in always:
        refusal = title_error(title, rule, repo)
        if refusal:
            return refusal, None
    if always:
        return None, None
    conditional = list(rules)
    first = conditional[0][0]
    if title_error(title, first, repo) is None:
        return None, None
    for rule, condition in conditional[1:]:
        if title_error(title, rule, repo) is None:
            return None, (
                f"Title checked: it breaks {first.rule} ({first.source}) and passes only the check "
                f"({rule.source}) that runs when {condition}; make sure that condition holds."
            )
    return title_error(title, first, repo), None


async def checked_title(
    client: Any,
    owner: str,
    name: str,
    ref: str,
    title: str,
    headers: dict[str, str],
    table_name: str = PULL_REQUEST,
) -> tuple[str | None, str | None]:
    """``(refusal, note)`` for *title* against the ``[table_name]`` rule on *ref*: a
    refusal stops the write; a note (a malformed rule) rides along with the write's result.

    A pull request title on a repository that declares no rule is checked
    against the title checks of its workflows; a workflow read never blocks."""
    rule, note = await read_title_rule(client, owner, name, ref, headers, table_name)
    if rule is None:
        if note or table_name != PULL_REQUEST:
            return None, (f"Title not checked: {note}" if note else None)
        try:
            rules = await _read_workflow_rules(client, owner, name, ref, headers)
        except Exception:  # best effort: an unreadable workflow never blocks the write
            rules = None
        return workflow_title_verdict(title, rules, f"{owner}/{name}")
    return title_error(title, rule, f"{owner}/{name}", table_name), None
