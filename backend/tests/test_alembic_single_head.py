"""Regression test for the Alembic single-head invariant.

Two migrations, ``c0a1p2v3e4r5_add_prompt_versions_table.py`` and
``d7e1a4b8c9f3_add_embedding_provider_model_columns.py``, both
declared ``down_revision = \"c3f8a1b9d4e2\"`` — a multi-head
configuration Alembic models as ambiguous and that ``alembic
upgrade head`` then refuses to apply (the operator has to run
``alembic merge`` first).  In a rolling deployment this manifests
as schema changes silently *not* being applied across a deploy.

The fix re-chained ``c0a1p2v3e4r5`` after ``d7e1a4b8c9f3`` so the
versioning chain has exactly one head.  This test pins the
invariant: a future migration accidentally branching off the same
ancestor will trip the assertion at CI time instead of slipping
through review.
"""

from __future__ import annotations

import ast
from pathlib import Path

VERSIONS_DIR = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions"
)


def _extract_literal(node: ast.AST) -> object:
    """Return the Python value of a literal expression node.

    Handles plain strings, ``None``, and tuples (used by merge
    migrations whose ``down_revision`` is a tuple of parents).
    """
    return ast.literal_eval(node)


def _parse_revisions() -> dict[str, object]:
    """Return ``{revision: down_revision}`` for every migration file.

    The ``down_revision`` value follows Alembic's conventions:

    * ``None``                          — the root of the chain.
    * ``"abc123..."`` (plain string)    — a regular migration with
      a single parent.
    * ``("abc", "def", ...)`` (tuple)   — a merge migration
      stitching multiple parent heads back into one.

    The AST-based parser avoids the brittle regex matching of an
    earlier version of this test (which choked on tuple-shaped
    ``down_revision`` values in merge migrations).
    """
    chain: dict[str, object] = {}
    for path in VERSIONS_DIR.glob("*.py"):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision: str | None = None
        down_revision: object = "<UNSET>"
        for stmt in tree.body:
            target_name: str | None = None
            value_node: ast.AST | None = None
            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) == 1 and isinstance(
                    stmt.targets[0], ast.Name
                ):
                    target_name = stmt.targets[0].id
                    value_node = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(
                stmt.target, ast.Name
            ):
                target_name = stmt.target.id
                value_node = stmt.value
            if target_name == "revision" and value_node is not None:
                rev_value = _extract_literal(value_node)
                assert isinstance(rev_value, str), (
                    f"`revision` in {path.name} must be a string; got "
                    f"{rev_value!r}"
                )
                revision = rev_value
            elif target_name == "down_revision" and value_node is not None:
                down_revision = _extract_literal(value_node)
        assert revision is not None, f"missing revision in {path.name}"
        assert down_revision != "<UNSET>", (
            f"missing down_revision in {path.name}"
        )
        chain[revision] = down_revision
    return chain


def _build_children_map(
    chain: dict[str, object],
) -> dict[object, list[str]]:
    """Map every parent revision to the list of revisions that descend
    from it.

    Merge migrations have a tuple ``down_revision`` — every element of
    that tuple is recorded as a separate parent edge, which is what
    Alembic itself does internally and what we want for the
    "at-most-one descendant" assertion below.
    """
    children: dict[object, list[str]] = {}
    for revision, down_revision in chain.items():
        parents: tuple[object, ...] = (
            tuple(down_revision)
            if isinstance(down_revision, tuple)
            else (down_revision,)
        )
        for parent in parents:
            children.setdefault(parent, []).append(revision)
    return children


def test_alembic_migration_chain_has_exactly_one_head():
    """The full migration graph must terminate in exactly one head
    (a revision with no children).

    Two heads are the actual configuration ``alembic upgrade head``
    refuses to apply without an explicit merge revision — and that's
    the failure mode (``c0a1p2v3e4r5`` and
    ``d7e1a4b8c9f3`` both branching from ``c3f8a1b9d4e2`` with no
    downstream merge produced two heads).  In rolling deployments
    that becomes the worst kind of silent failure: the schema
    changes declared after the branch point never reach production
    until someone hand-rolls a merge migration.

    Note: this codebase legitimately uses merge migrations (a revision
    whose ``down_revision`` is a tuple of parents), so an interior
    branch is fine *provided* the branch is stitched back together by
    a merge later in the chain.  The "exactly one head" invariant is
    what matters — interior branching alone is not a problem.
    """
    chain = _parse_revisions()
    children = _build_children_map(chain)

    heads = [rev for rev in chain if rev not in children]
    assert len(heads) == 1, (
        "Alembic migration chain must have exactly one head; "
        f"observed {len(heads)} heads: {sorted(heads)!r}.  Either "
        "rebase the divergent migration onto the existing head, or "
        "add an explicit merge migration that stitches all current "
        "heads back into one."
    )
