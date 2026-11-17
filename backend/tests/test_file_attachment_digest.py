"""``FileAttachment.sha256`` is the digest of the object the row names.

Two readers trust the column without reading the object: the tool-file dedup
(``builtin_tools._shared._find_existing_tool_file``) and the presentation
package import, which tells a template's same-name assets apart by it
(``presentation_package._namesake_holding``). A write that points an existing
row at new bytes and leaves the column alone lets the import pick the wrong
logo with no read to catch it - the template asset replace route and the brand
seed refresh both did.

So every write of ``s3_object_name`` sets ``sha256`` with it: the digest of the
bytes just stored, or ``None`` where the writer does not hold them. These tests
read the source of ``backend/app`` and fail on a write that does not - an
attribute assignment or ``setattr`` on an existing row (same function, same
object), a bulk ``update(...).values(...)``, or a ``FileAttachment(...)``
constructor. The first two go by the attribute's name, not the object's type:
``KBExportPackage`` also has an ``s3_object_name``, and rewriting one in place
(nothing does today) would need an exemption here, with its reason.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tests import app_sources

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

KEY = "s3_object_name"
DIGEST = "sha256"

#: ``FileAttachment(...)`` calls that create a row without recording its digest,
#: from before the rule. FROZEN: never add to it - record the digest of the
#: bytes you just stored (``sha256=``) instead. A NEW row with no digest is not
#: wrong, only unused: neither reader matches a NULL, and the tool path among
#: these (``generate_image``) is kept out of the tool-file dedup on purpose (see
#: ``_persist_generated_file``). An entry whose site records the digest must be
#: removed (``test_the_legacy_list_only_names_sites_that_still_need_it``).
LEGACY_NEW_ROWS_WITHOUT_DIGEST = frozenset(
    {
        ("app/api/v1/chat.py", "upload_file_to_conversation"),
        ("app/api/v1/custom_tools.py", "upload_tool_test_file"),
        ("app/api/v1/datasets.py", "upload_entry_file"),
        ("app/api/v1/flow_runs.py", "upload_flow_run_file"),
        ("app/channels/router.py", "_resolve_channel_files"),
        ("app/services/builtin_tools/media.py", "build_image_generation_tool._generate_image"),
    }
)


@dataclass(frozen=True)
class _Write:
    """One place that writes ``s3_object_name``."""

    path: str
    scope: str
    line: int
    #: ``attribute`` / ``setattr`` (an existing row), ``values`` (a bulk
    #: update), ``constructor`` (a new ``FileAttachment``).
    kind: str
    #: The row expression, or the call, as written.
    target: str
    records_digest: bool

    def __str__(self) -> str:
        return f"{self.path}:{self.line} in {self.scope}: {self.kind} write of {KEY} on {self.target}"


def _iter_scopes(owner: ast.AST, qualname: str) -> Iterator[tuple[str, list[ast.AST]]]:
    """``(qualname, nodes)`` for *owner* and every function / class nested in it.

    A scope's nodes stop at a nested ``def`` / ``class``: a digest written in an
    inner function does not answer a key written in its caller, or the reverse.
    """
    own: list[ast.AST] = []
    nested: list[tuple[ast.AST, str]] = []
    stack = list(ast.iter_child_nodes(owner))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            nested.append((node, node.name if qualname == "<module>" else f"{qualname}.{node.name}"))
            continue
        own.append(node)
        stack.extend(ast.iter_child_nodes(node))
    yield qualname, own
    for child, name in nested:
        yield from _iter_scopes(child, name)


def _assigned_attributes(node: ast.AST) -> Iterator[ast.Attribute]:
    if isinstance(node, ast.Assign):
        targets: list[ast.AST] = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    else:
        return
    while targets:
        target = targets.pop()
        if isinstance(target, ast.Tuple | ast.List):
            targets.extend(target.elts)
        elif isinstance(target, ast.Starred):
            targets.append(target.value)
        elif isinstance(target, ast.Attribute):
            yield target


def _setattr_call(node: ast.AST) -> tuple[ast.AST, str] | None:
    """``(object, attribute)`` of ``setattr(obj, "name", value)``, else ``None``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[0], node.args[1].value
    return None


def _call_field_names(call: ast.Call) -> tuple[set[str], bool]:
    """The field names a call passes (keywords and dict-literal keys) and
    whether it also unpacks something the source does not spell out."""
    names = {kw.arg for kw in call.keywords if kw.arg is not None}
    opaque = any(kw.arg is None for kw in call.keywords)
    for arg in call.args:
        if isinstance(arg, ast.Dict):
            for key in arg.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
                elif key is None:
                    opaque = True
    return names, opaque


def _file_attachment_names(tree: ast.Module) -> set[str]:
    """The names ``FileAttachment`` is bound to in this module (aliases included)."""
    names = {"FileAttachment"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "FileAttachment":
                    names.add(alias.asname or alias.name)
    return names


def _is_file_attachment_call(call: ast.Call, names: set[str]) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id in names) or (
        isinstance(func, ast.Attribute) and func.attr == "FileAttachment"
    )


def _writes_in_source(
    source: str, path: str, companion: str = DIGEST, tree: ast.Module | None = None
) -> list[_Write]:
    """Every write of ``s3_object_name`` in *source*; ``records_digest`` says
    whether it sets *companion* with it (the digest, or another field that
    describes the bytes the key names)."""
    tree = ast.parse(source, filename=path) if tree is None else tree
    constructors = _file_attachment_names(tree)
    writes: list[_Write] = []
    for scope, nodes in _iter_scopes(tree, "<module>"):
        keyed: list[tuple[str, ast.AST, str]] = []
        digested: set[str] = set()
        for node in nodes:
            for attribute in _assigned_attributes(node):
                row = ast.unparse(attribute.value)
                if attribute.attr == KEY:
                    keyed.append((row, node, "attribute"))
                elif attribute.attr == companion:
                    digested.add(row)
            called = _setattr_call(node)
            if called is not None:
                row = ast.unparse(called[0])
                if called[1] == KEY:
                    keyed.append((row, node, "setattr"))
                elif called[1] == companion:
                    digested.add(row)
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                fields, _opaque = _call_field_names(node)
                if KEY in fields:
                    writes.append(
                        _Write(path, scope, node.lineno, "values", ast.unparse(node.func), companion in fields)
                    )
            elif _is_file_attachment_call(node, constructors):
                fields, opaque = _call_field_names(node)
                if KEY in fields or opaque:
                    writes.append(
                        _Write(path, scope, node.lineno, "constructor", ast.unparse(node.func), companion in fields)
                    )
        writes.extend(
            _Write(path, scope, node.lineno, kind, row, row in digested) for row, node, kind in keyed
        )
    return writes


@functools.cache
def _app_writes(companion: str = DIGEST) -> tuple[_Write, ...]:
    writes: list[_Write] = []
    for file in app_sources.python_files(APP_ROOT):
        source = app_sources.source(file)
        if KEY not in source and "FileAttachment" not in source:
            continue
        writes.extend(
            _writes_in_source(source, file.relative_to(BACKEND_ROOT).as_posix(), companion, app_sources.tree(file))
        )
    return tuple(writes)


def _is_legacy(write: _Write) -> bool:
    return write.kind == "constructor" and (write.path, write.scope) in LEGACY_NEW_ROWS_WITHOUT_DIGEST


def test_every_write_of_an_object_key_sets_the_digest_with_it():
    writes = _app_writes()
    # The walk reached the writers: a scan that parses nothing passes vacuously.
    assert sum(w.kind == "constructor" for w in writes) >= 10, "the scan no longer sees the FileAttachment writers"

    offenders = [w for w in writes if not w.records_digest and not _is_legacy(w)]
    assert not offenders, (
        "These writes point a FileAttachment at an object without saying what it holds. Set "
        f"`{DIGEST}` with it - in the same call, or on the same row in the same function: the SHA-256 "
        "of the bytes just stored (for a template asset, `template_containers.template_asset_sha256(stored)`), "
        "or `None` only where the writer does not hold them. The presentation package import trusts the "
        "column to tell a template's same-name assets apart without reading them.\n"
        + "\n".join(str(w) for w in offenders)
    )


def test_the_legacy_list_only_names_sites_that_still_need_it():
    """The list shrinks, it never grows: a site that now records the digest, or
    that moved, leaves an entry that would silently cover the next new writer."""
    still_needed = {(w.path, w.scope) for w in _app_writes() if _is_legacy(w) and not w.records_digest}
    stale = sorted(LEGACY_NEW_ROWS_WITHOUT_DIGEST - still_needed)
    assert not stale, f"Remove these entries from LEGACY_NEW_ROWS_WITHOUT_DIGEST: {stale}"


#: Where a template asset - or a deck package's copy of one - is created or
#: pointed at new bytes.
TEMPLATE_ASSET_WRITE_SITES = (
    ("app/api/v1/template_containers.py", "add_template_asset"),
    ("app/api/v1/template_containers.py", "replace_template_asset"),
    ("app/api/v1/template_containers.py", "_persist_imported_package"),
    ("app/services/builtin_tools/template_authoring.py", "build_template_authoring_tools._attach_template_asset"),
    ("app/services/template_asset_seed.py", "_sync_container_assets"),
    ("app/services/presentation_package.py", "_resolve_asset"),
)


def test_every_template_asset_write_records_its_digest():
    """What the import reads: no template asset row may be created or rewritten
    without its digest, whatever the legacy list says about other rows."""
    for site in TEMPLATE_ASSET_WRITE_SITES:
        at_site = [w for w in _app_writes() if (w.path, w.scope) == site]
        assert at_site, f"{site} no longer writes {KEY}: update this list"
        assert all(w.records_digest for w in at_site), [str(w) for w in at_site if not w.records_digest]


def test_every_template_asset_write_records_its_geometry():
    """A logo wall crops a template asset by the trim box on its row,
    served with a revision of the object the row names. A write that points
    the row at new bytes and leaves ``image_meta`` alone crops the new logo
    with the old one's box, and a stale box is never read again (the deck-save
    backfill only reads a NULL). So every such write sets ``image_meta`` with
    the key: read from the bytes just stored, or ``None`` - never copied from
    another row."""
    for site in TEMPLATE_ASSET_WRITE_SITES:
        at_site = [w for w in _app_writes("image_meta") if (w.path, w.scope) == site]
        assert at_site, f"{site} no longer writes {KEY}: update this list"
        assert all(w.records_digest for w in at_site), [str(w) for w in at_site if not w.records_digest]
    # The scan sees a key written without the geometry.
    (write,) = _writes_in_source("def write():\n    row.s3_object_name = key\n    row.sha256 = d\n", "case.py", "image_meta")
    assert not write.records_digest


def test_the_checker_catches_every_shape_of_write():
    """The scan itself: each way of pointing a row at an object is seen, and is
    only accepted with the digest written next to it."""
    cases = {
        "row.s3_object_name = key": False,
        "row.s3_object_name = key\nrow.sha256 = digest": True,
        "row.s3_object_name = key\nrow.sha256 = None": True,
        "row.s3_object_name = key\nother.sha256 = digest": False,
        "row.s3_object_name, row.size = key, 3\nrow.sha256 = digest": True,
        "setattr(row, 's3_object_name', key)": False,
        "setattr(row, 's3_object_name', key)\nsetattr(row, 'sha256', digest)": True,
        "update(FileAttachment).values(s3_object_name=key)": False,
        "update(FileAttachment).values(s3_object_name=key, sha256=digest)": True,
        "update(FileAttachment).values({'s3_object_name': key})": False,
        "FileAttachment(filename=name, s3_object_name=key)": False,
        "FileAttachment(filename=name, s3_object_name=key, sha256=digest)": True,
        "FileAttachment(**fields)": False,
        "models.FileAttachment(s3_object_name=key)": False,
    }
    for body, accepted in cases.items():
        source = "def write():\n" + "\n".join(f"    {line}" for line in body.splitlines())
        (write,) = _writes_in_source(source, "case.py")
        assert write.records_digest is accepted, body

    aliased = "from app.models.file_attachment import FileAttachment as FA\n\ndef write():\n    FA(s3_object_name=key)\n"
    (write,) = _writes_in_source(aliased, "case.py")
    assert write.kind == "constructor" and not write.records_digest

    # A digest written in a nested function answers nothing in its caller.
    nested = (
        "def write():\n"
        "    row.s3_object_name = key\n"
        "    def later():\n"
        "        row.sha256 = digest\n"
    )
    (write,) = _writes_in_source(nested, "case.py")
    assert write.scope == "write" and not write.records_digest
    assert _writes_in_source("def read():\n    return row.s3_object_name\n", "case.py") == []
