"""Every security audit finding carries a stable id.

A title cannot identify a finding: several embed a count ("3 integration(s) in
error state"). The Settings > Access and security screen, and any other reader of
``GET /api/v1/security-audit``, keys a finding by its ``id`` instead:
``<category>.<name>``, the same from one run to the next.

The model keeps ``id`` optional, so a report built by a check that sets none
still validates. This file is what makes it present: it reads the module's own
source and refuses an ``AuditFinding(...)`` without a literal ``id=`` or a
literal ``category=``, an id outside its category, an id two findings share,
and a finding built any other way than a direct ``AuditFinding(...)`` call,
whose id the scan cannot read. Each refusal names the line, the check it is
in and what to write.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections import defaultdict
from dataclasses import dataclass

from app.api.v1 import security_audit

_ID_SHAPE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_MODULE = "security_audit.py"
_RULE = (
    "Every AuditFinding(...) names a literal, stable '<category>.<name>' id, for example "
    'id="agents.tool_deny_lists": the Settings > Access and security screen and API readers identify findings by it, '
    "never by their title. A computed or conditional id or category cannot be checked: build one AuditFinding "
    "per case."
)


@dataclass(frozen=True)
class _Finding:
    line: int
    check: str
    id: str | None
    category: str | None
    title: str

    def where(self) -> str:
        return f"{_MODULE} line {self.line} in {self.check} (title {self.title!r})"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _literal(call: ast.Call, name: str) -> str | None:
    value = _keyword(call, name)
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _text(value: ast.expr | None) -> str:
    """A title as written: an f-string's fields as ``{...}``, both branches of a conditional."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{...}" for part in value.values)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _text(value.left) + _text(value.right)
    if isinstance(value, ast.IfExp):
        return f"{_text(value.body)} | {_text(value.orelse)}"
    return "{...}"


def _title(call: ast.Call) -> str:
    return _text(_keyword(call, "title"))


def _names_finding(node: ast.AST) -> bool:
    """``AuditFinding``, bare or qualified (``security_audit.AuditFinding``)."""
    return (isinstance(node, ast.Name) and node.id == "AuditFinding") or (
        isinstance(node, ast.Attribute) and node.attr == "AuditFinding"
    )


def _is_finding_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _names_finding(node.func)


def _scan(source: str) -> tuple[list[_Finding], list[str]]:
    """The findings *source* builds, and every other use of ``AuditFinding`` the scan cannot see through."""
    tree = ast.parse(source)
    check_of: dict[ast.AST, str] = {}
    # ast.walk is breadth-first: a nested function is visited after the one
    # around it, so the innermost name wins.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                check_of[child] = node.name

    # The call itself, a type (an annotation, a response model) and an
    # isinstance() check are the uses that build nothing the scan misses.
    seen_through: set[int] = set()
    for node in ast.walk(tree):
        types: list[ast.expr] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every_argument = (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            )
            types += [argument.annotation for argument in every_argument if argument and argument.annotation]
            if node.returns is not None:
                types.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            types.append(node.annotation)
        elif isinstance(node, ast.Call):
            if _is_finding_call(node):
                seen_through.add(id(node.func))
            if isinstance(node.func, ast.Name) and node.func.id in {"isinstance", "issubclass"}:
                types += node.args[1:]
            types += [keyword.value for keyword in node.keywords if keyword.arg == "response_model"]
        for annotation in types:
            seen_through.update(id(child) for child in ast.walk(annotation))

    findings = sorted(
        (
            _Finding(
                line=node.lineno,
                check=check_of.get(node, "the module body"),
                id=_literal(node, "id"),
                category=_literal(node, "category"),
                title=_title(node),
            )
            for node in ast.walk(tree)
            if _is_finding_call(node)
        ),
        key=lambda finding: finding.line,
    )
    # An import under another name hides every call made through it; any other
    # read of the name hands the class to code that builds what the scan
    # cannot read.
    renamed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(alias.name.rpartition(".")[2] == "AuditFinding" and alias.asname for alias in node.names)
    ]
    used = [
        node
        for node in ast.walk(tree)
        if _names_finding(node) and isinstance(node.ctx, ast.Load) and id(node) not in seen_through
    ]
    hidden = [
        f"{_MODULE} line {node.lineno} in {check_of.get(node, 'the module body')}: build the finding with a direct "
        "AuditFinding(id=..., ...) call - AuditFinding.model_validate(), an alias, an import under another name or "
        "AuditFinding passed as a value builds a finding whose id this scan cannot read"
        for node in sorted((*renamed, *used), key=lambda node: (node.lineno, node.col_offset))
    ]
    return findings, hidden


def _missing_ids(findings: list[_Finding]) -> list[str]:
    return [
        f'{finding.where()}: add id="{finding.category or "<category>"}.<name>"'
        for finding in findings
        if finding.id is None
    ]


def _misplaced_ids(findings: list[_Finding]) -> list[str]:
    wrong = []
    for finding in findings:
        if finding.category is None:
            # Before the id check: a finding with neither literal is told about both in one run.
            named = finding.id is not None and _ID_SHAPE.match(finding.id) is not None
            prefix = finding.id.partition(".")[0] if named else "<category>"
            subject = f"id {finding.id!r}" if finding.id is not None else "its id"
            wrong.append(
                f"{finding.where()}: its category is not a literal, so {subject} cannot be checked against it - "
                f'write category="{prefix}", or build one AuditFinding per category'
            )
            continue
        if finding.id is None:
            continue
        if _ID_SHAPE.match(finding.id) and finding.id.startswith(f"{finding.category}."):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", finding.id.rpartition(".")[2].lower()).strip("_")
        # The id suggested must pass this check in turn: its name starts with a letter.
        if not re.match(r"[a-z]", name):
            name = "<name>"
        expected = f"{finding.category}.{name}"
        wrong.append(
            f"{finding.where()}: id {finding.id!r} is not '<category>.<snake_case name>' for its category "
            f'{finding.category!r} - write id="{expected}"'
        )
    return wrong


def _shared_ids(findings: list[_Finding]) -> list[str]:
    users: dict[str, list[_Finding]] = defaultdict(list)
    for finding in findings:
        if finding.id is not None:
            users[finding.id].append(finding)
    return [
        f"id {finding_id!r} is used by "
        + " and ".join(f"line {finding.line} in {finding.check}" for finding in shared)
        + f" of {_MODULE}: give each finding a name of its own"
        for finding_id, shared in sorted(users.items())
        if len(shared) > 1
    ]


def _module_scan() -> tuple[list[_Finding], list[str]]:
    return _scan(inspect.getsource(security_audit))


def test_the_source_scan_sees_the_findings_the_audit_builds():
    findings, _hidden = _module_scan()
    ids = {finding.id for finding in findings}
    # Guards the scan itself: were AuditFinding renamed or imported under
    # another name, every check below would pass on an empty list.
    assert {
        "encryption.secret_key_configured",
        "infrastructure.redis_healthy",
        "network.proxy_trust_missing",
    } <= ids


def test_every_finding_names_a_literal_id():
    findings, _hidden = _module_scan()
    missing = _missing_ids(findings)
    assert missing == [], "\n".join([_RULE, *missing])


def test_a_finding_id_is_namespaced_by_its_category():
    findings, _hidden = _module_scan()
    wrong = _misplaced_ids(findings)
    assert wrong == [], "\n".join([_RULE, *wrong])


def test_no_two_findings_share_an_id():
    findings, _hidden = _module_scan()
    shared = _shared_ids(findings)
    assert shared == [], "\n".join([_RULE, *shared])


def test_every_finding_is_built_where_the_scan_can_read_its_id():
    _findings, hidden = _module_scan()
    assert hidden == [], "\n".join([_RULE, *hidden])


def test_the_client_address_findings_keep_their_ids():
    # The Security options card links TRUST_PROXY_HEADERS to them.
    findings, _hidden = _module_scan()
    built_by = {finding.id: finding.check for finding in findings}
    ids = (
        "network.client_addresses_undeclared_proxy",
        "network.client_addresses_declared_proxy",
        "network.client_addresses_no_undeclared_proxy",
        "network.client_addresses_check_skipped",
    )
    assert {finding_id: built_by.get(finding_id) for finding_id in ids} == dict.fromkeys(
        ids, "_check_forwarded_client_addresses"
    )


_SAMPLE = """\
from app.api.v1 import security_audit
from app.api.v1.security_audit import AuditFinding
from app.api.v1.security_audit import AuditFinding as Finding


def _check_sample(findings: list[AuditFinding], count: int, flag: bool, kind: str) -> None:
    findings.append(
        AuditFinding(
            category="tools",
            severity="warning",
            title=f"{count} runner(s) read without a bound",
            description="d",
        )
    )
    findings.append(AuditFinding(id="agents.misfiled", category="tools", severity="info", title="Misfiled", description="d"))
    findings.append(AuditFinding(id="tools.2fa_off", category="tools", severity="info", title="Digit", description="d"))
    findings.append(AuditFinding(id="agents.computed", category=kind, severity="info", title="Computed", description="d"))
    findings.append(AuditFinding(category=kind, severity="info", title="Unnamed", description="d"))
    findings.append(AuditFinding(id="tools.twice", category="tools", severity="pass", title="Once", description="d"))
    findings.append(AuditFinding(id="tools.twice", category="tools", severity="pass", title="Twice", description="d"))
    findings.append(
        AuditFinding(id="tools.on" if flag else "tools.off", category="tools", severity="info", title="Either", description="d")
    )
    findings.append(security_audit.AuditFinding(category="agents", severity="info", title="Qualified", description="d"))
    findings.append(Finding(category="agents", severity="info", title="Renamed", description="d"))
    if isinstance(findings[0], AuditFinding):
        findings.append(AuditFinding.model_validate({"category": "tools", "severity": "info", "title": "t", "description": "d"}))
    build = AuditFinding
"""


def _sample_line(fragment: str, occurrence: int = 1) -> int:
    lines = [number for number, text in enumerate(_SAMPLE.splitlines(), start=1) if fragment in text]
    return lines[occurrence - 1]


def test_a_refusal_names_the_line_the_check_and_the_id_to_write():
    findings, hidden = _scan(_SAMPLE)
    multi_line = _sample_line("AuditFinding(")
    conditional = _sample_line('title="Either"')
    qualified = _sample_line('title="Qualified"')
    misfiled = _sample_line("agents.misfiled")
    digit = _sample_line("tools.2fa_off")
    computed = _sample_line("agents.computed")
    unnamed = _sample_line('title="Unnamed"')
    once, twice = _sample_line("tools.twice", 1), _sample_line("tools.twice", 2)
    # The multi-line call is reported at its first line, with its category
    # filled in; the conditional id is refused as not literal; a qualified
    # call is read like a bare one.
    assert _missing_ids(findings) == [
        f"{_MODULE} line {multi_line} in _check_sample (title '{{...}} runner(s) read without a bound'): "
        'add id="tools.<name>"',
        f"{_MODULE} line {unnamed} in _check_sample (title 'Unnamed'): add id=\"<category>.<name>\"",
        f"{_MODULE} line {conditional} in _check_sample (title 'Either'): add id=\"tools.<name>\"",
        f"{_MODULE} line {qualified} in _check_sample (title 'Qualified'): add id=\"agents.<name>\"",
    ]
    # The id suggested is one the check accepts; a computed category is
    # refused with or without an id, since no id can be checked against it.
    assert _misplaced_ids(findings) == [
        f"{_MODULE} line {misfiled} in _check_sample (title 'Misfiled'): id 'agents.misfiled' "
        "is not '<category>.<snake_case name>' for its category 'tools' - write id=\"tools.misfiled\"",
        f"{_MODULE} line {digit} in _check_sample (title 'Digit'): id 'tools.2fa_off' "
        "is not '<category>.<snake_case name>' for its category 'tools' - write id=\"tools.<name>\"",
        f"{_MODULE} line {computed} in _check_sample (title 'Computed'): its category is not a literal, so id "
        "'agents.computed' cannot be checked against it - write category=\"agents\", or build one AuditFinding "
        "per category",
        f"{_MODULE} line {unnamed} in _check_sample (title 'Unnamed'): its category is not a literal, so its id "
        'cannot be checked against it - write category="<category>", or build one AuditFinding per category',
    ]
    assert _shared_ids(findings) == [
        f"id 'tools.twice' is used by line {once} in _check_sample and line {twice} in _check_sample "
        f"of {_MODULE}: give each finding a name of its own"
    ]
    # The annotation and the isinstance() check build nothing; the import
    # under another name (which hides the call made through it), the
    # model_validate() call and the alias build what the scan cannot read.
    assert [entry.split(":")[0] for entry in hidden] == [
        f"{_MODULE} line {_sample_line('AuditFinding as Finding')} in the module body",
        f"{_MODULE} line {_sample_line('model_validate')} in _check_sample",
        f"{_MODULE} line {_sample_line('build = AuditFinding')} in _check_sample",
    ]


async def test_the_report_carries_the_id(monkeypatch):
    monkeypatch.setattr(security_audit.settings, "secret_key", "s" * 48)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_encryption(findings)
    assert [f.model_dump()["id"] for f in findings] == ["encryption.secret_key_configured"]


def test_a_finding_without_an_id_still_validates():
    finding = security_audit.AuditFinding(category="mcp", severity="info", title="t", description="d")
    assert finding.id is None
    assert finding.model_dump()["id"] is None
