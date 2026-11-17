"""Validate Alembic migration history integrity (no database required).

Checks performed:
1. No duplicate revision IDs
2. No orphaned migrations (down_revision references a non-existent revision)
3. Exactly one head (single linear tip after merges)
4. Connected DAG from base(s) to head
5. At least one base migration
6. Lock safety: every NEW migration that emits lock-taking DDL must set a
   ``lock_timeout`` safety belt (see ``test_lock_taking_ddl_sets_lock_timeout``)
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Squash baseline: the entire migration history was collapsed
# into a single baseline that recreates the full schema from
# ``Base.metadata``.  A baseline only ever executes against an EMPTY database
# (fresh installs ``create_all`` + ``stamp head`` — the body never runs;
# existing installs are re-stamped onto the baseline per the release notes),
# so its ``op.create_table`` / ``op.create_index`` / ``op.drop_*`` statements
# can never contend for locks with live traffic.  It is therefore exempt from
# the lock-timeout belt requirement below.
_BASELINE_REVISIONS: frozenset[str] = frozenset({
    "d046bcdf345f_baseline_schema.py",
})


def _extract_assignments(filepath: Path) -> dict[str, ast.expr]:
    """Return top-level assignment targets from a migration file."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    assignments: dict[str, ast.expr] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments[node.target.id] = node.value
    return assignments


def _eval_literal(node: ast.expr) -> str | tuple | None:
    """Safely evaluate an AST node to a string literal, tuple of strings, or None.

    Raises ValueError for unsupported expression shapes.
    """
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, str):
            return node.value
        raise ValueError(
            f"unsupported constant type {type(node.value).__name__} (expected str or None)"
        )
    if isinstance(node, ast.Tuple):
        return tuple(_eval_literal(elt) for elt in node.elts)
    raise ValueError(f"unsupported AST node type {type(node).__name__}")


def parse_migration(filepath: Path) -> dict:
    """Extract revision metadata from a single migration file."""
    assigns = _extract_assignments(filepath)

    revision = _eval_literal(assigns["revision"]) if "revision" in assigns else None
    down_raw = _eval_literal(assigns["down_revision"]) if "down_revision" in assigns else None

    if revision is not None and not isinstance(revision, str):
        raise ValueError(f"revision is not a string: {revision!r}")

    if down_raw is None:
        down_revisions: tuple[str, ...] = ()
    elif isinstance(down_raw, str):
        down_revisions = (down_raw,)
    elif isinstance(down_raw, tuple):
        for entry in down_raw:
            if not isinstance(entry, str):
                raise ValueError(
                    f"down_revision tuple contains non-string entry: {entry!r}"
                )
        down_revisions = down_raw  # type: ignore[assignment]
    else:
        raise ValueError(f"down_revision is not str/tuple/None: {down_raw!r}")

    return {
        "revision": revision,
        "down_revisions": down_revisions,
        "file": filepath.name,
    }


@pytest.fixture(scope="module")
def migrations():
    """Parse all migration files and return the list + any parse errors."""
    migration_files = sorted(VERSIONS_DIR.glob("*.py"))
    assert migration_files, f"No migration files found in {VERSIONS_DIR}"

    errors: list[str] = []
    parsed: list[dict] = []
    for f in migration_files:
        if f.name.startswith("__"):
            continue
        try:
            m = parse_migration(f)
        except Exception as exc:
            errors.append(f"Failed to parse {f.name}: {exc}")
            continue
        # A non-dunder ``*.py`` file in ``alembic/versions/`` with no
        # ``revision`` assignment (or a non-string one) is a malformed
        # migration script.  Fail loudly so a half-written file can't
        # silently slip past the DAG checks below — historically a
        # `continue` here hid that exact bug.
        if not isinstance(m["revision"], str) or not m["revision"]:
            errors.append(
                f"{f.name}: missing or non-string `revision = …` assignment"
            )
            continue
        parsed.append(m)

    assert not errors, (
        f"{len(errors)} migration file(s) failed to parse:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )
    return parsed


def test_no_duplicate_revisions(migrations):
    """No two migration files should share the same revision ID."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for m in migrations:
        rev = m["revision"]
        if rev in seen:
            duplicates.append(
                f"Duplicate '{rev}' in {m['file']} (already in {seen[rev]})"
            )
        else:
            seen[rev] = m["file"]
    assert not duplicates, "\n".join(duplicates)


def test_no_orphaned_down_revisions(migrations):
    """Every down_revision must reference an existing revision."""
    all_revisions = {m["revision"] for m in migrations}
    orphans: list[str] = []
    for m in migrations:
        for dep in m["down_revisions"]:
            if dep not in all_revisions:
                orphans.append(f"{m['file']}: down_revision '{dep}' does not exist")
    assert not orphans, "\n".join(orphans)


def test_single_head(migrations):
    """There must be exactly one head revision (no unmerged branches)."""
    all_revisions = {m["revision"] for m in migrations}
    children: dict[str, list[str]] = {rev: [] for rev in all_revisions}
    for m in migrations:
        for dep in m["down_revisions"]:
            if dep in children:
                children[dep].append(m["revision"])

    heads = [rev for rev, kids in children.items() if not kids]
    assert len(heads) > 0, "No head revision found (cyclic dependency?)"

    if len(heads) > 1:
        seen = {m["revision"]: m["file"] for m in migrations}
        details = ", ".join(f"'{h}' ({seen.get(h, '?')})" for h in sorted(heads))
        pytest.fail(
            f"Multiple heads detected ({len(heads)}): {details}. "
            f"Create a merge migration to resolve."
        )


def test_connected_dag(migrations):
    """All revisions must be reachable from the head (connected graph)."""
    all_revisions = {m["revision"] for m in migrations}
    children: dict[str, list[str]] = {rev: [] for rev in all_revisions}
    for m in migrations:
        for dep in m["down_revisions"]:
            if dep in children:
                children[dep].append(m["revision"])

    heads = [rev for rev, kids in children.items() if not kids]
    if len(heads) != 1:
        pytest.skip("Cannot verify DAG connectivity without a single head")

    rev_to_deps = {m["revision"]: m["down_revisions"] for m in migrations}
    visited: set[str] = set()
    stack = [heads[0]]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for dep in rev_to_deps.get(current, ()):
            stack.append(dep)

    unreachable = all_revisions - visited
    if unreachable:
        seen = {m["revision"]: m["file"] for m in migrations}
        details = ", ".join(f"'{r}' ({seen.get(r, '?')})" for r in sorted(unreachable))
        pytest.fail(f"{len(unreachable)} unreachable revision(s): {details}")


def test_has_base_migration(migrations):
    """At least one migration must have no down_revision (the root)."""
    bases = [m for m in migrations if not m["down_revisions"]]
    assert bases, "No base migration found (every migration has a down_revision)"


def _sole_head(migrations) -> str:
    """Return the single head revision, failing if there is not exactly one."""
    all_revisions = {m["revision"] for m in migrations}
    children: dict[str, list[str]] = {rev: [] for rev in all_revisions}
    for m in migrations:
        for dep in m["down_revisions"]:
            if dep in children:
                children[dep].append(m["revision"])
    heads = [rev for rev, kids in children.items() if not kids]
    assert len(heads) == 1, f"expected exactly one head, found {sorted(heads)}"
    return heads[0]


def test_squash_metadata_matches_history(migrations):
    """The squash compatibility constants must stay in sync with the real history.

    ``_reconcile_pre_squash_revision`` trusts exactly two constants.  If either drifts
    from the actual Alembic script directory, existing deployments could fail to
    boot (or heal to the wrong marker), so pin them here:

    - ``BASELINE_REVISION`` must be the single ROOT of the current history
      (the squashed baseline). Post-squash migrations chain on top of it, so
      the head moves to baseline+N over time — ``_reconcile_pre_squash_revision``
      deliberately stamps the *baseline* (not ``head``) so those pending
      migrations still run after the auto-heal.
    - ``PRE_SQUASH_FINAL_HEAD`` must NOT exist in the current history (it was
      deleted by the squash) and must differ from the baseline — otherwise the
      auto-heal decision is meaningless.
    """
    from app.core._pre_squash_revisions import (
        BASELINE_REVISION,
        PRE_SQUASH_FINAL_HEAD,
    )

    all_revisions = {m["revision"] for m in migrations}
    # Single-head invariant still holds (also enforced by test_single_head).
    _sole_head(migrations)

    roots = [m["revision"] for m in migrations if not m["down_revisions"]]
    assert roots == [BASELINE_REVISION], (
        f"BASELINE_REVISION '{BASELINE_REVISION}' must be the single root of "
        f"the Alembic history; found root(s) {sorted(roots)}. Update "
        "app/core/_pre_squash_revisions.py."
    )
    assert PRE_SQUASH_FINAL_HEAD != BASELINE_REVISION, (
        "PRE_SQUASH_FINAL_HEAD must differ from BASELINE_REVISION."
    )
    assert PRE_SQUASH_FINAL_HEAD not in all_revisions, (
        f"PRE_SQUASH_FINAL_HEAD '{PRE_SQUASH_FINAL_HEAD}' still exists in the "
        "current history; it must be a deleted pre-squash revision."
    )


# ---------------------------------------------------------------------------
# Lock safety (2026-07-05 production outage post-mortem)
#
# ``ALTER TABLE`` needs a brief ``ACCESS EXCLUSIVE`` lock even for
# metadata-only DDL.  During a rolling deploy, previous-version pods hold
# long-lived open transactions (agentic loops keep DB sessions open for
# minutes) whose ``ACCESS SHARE`` locks block the ALTER; PostgreSQL queues
# fairly, so every subsequent reader queues behind the blocked DDL and the
# lock queue never empties.  Without a ``lock_timeout`` the migration wedges
# startup; with one it fails fast (SQLSTATE 55P03) and the startup runner
# (``app/core/migrations.py``) retries with backoff.
#
# There is NO cold table: ``platform_settings`` — a singleton row — took
# production down exactly this way (migration ``p1m2c3s4v5r6``, 2026-07-05).
#
# This test makes the safety belt machine-enforced: every migration that
# emits lock-taking DDL must reference ``lock_timeout`` — in practice by
# calling ``set_lock_timeout``/``terminate_lock_holders`` from
# ``app.core.migration_ddl`` (the canonical recipe) or by setting
# ``SET LOCAL lock_timeout`` directly; ``create_index_in_place`` sets it for
# an index.  ``CREATE/DROP INDEX CONCURRENTLY`` is not lock-taking here: it
# waits on snapshots, not on a lock, and a new migration may not run it at all
# (``test_no_new_migration_builds_an_index_concurrently``).
#
# Migrations that predate the rule are grandfathered below.  DO NOT add new
# entries to the grandfather list — fix the migration instead (see
# dev-docs/reference/migrations-safety.md).
# ---------------------------------------------------------------------------

_LOCK_TAKING_RE = re.compile(
    r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b|\bALTER\s+TYPE\b"
    r"|op\.(add_column|drop_column|alter_column"
    r"|drop_table|rename_table|create_unique_constraint|create_foreign_key"
    r"|drop_constraint|create_primary_key)\b",
    re.I,
)

# Plain (non-CONCURRENTLY) index DDL also takes contended locks.
_PLAIN_INDEX_RE = re.compile(
    r"\b(CREATE|DROP)\s+(UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY)", re.I
)

# Alembic index ops (``op.create_index`` / ``op.drop_index``) are handled
# separately from ``_LOCK_TAKING_RE``: an in-place build takes a contended
# lock, while the ``postgresql_concurrently=True`` variant waits on snapshots
# instead (banned in new migrations, see above).  The exemption is
# file-scoped on purpose — this guard is presence-based, and raw
# ``CREATE/DROP INDEX`` SQL is still caught by ``_PLAIN_INDEX_RE`` regardless.
_ALEMBIC_INDEX_OP_RE = re.compile(r"op\.(create_index|drop_index)\b")
_ALEMBIC_CONCURRENT_KWARG_RE = re.compile(
    r"postgresql_concurrently\s*=\s*True"
)

# A FOREIGN KEY takes a SHARE ROW EXCLUSIVE lock on the table it REFERENCES,
# even when it is declared inline in a brand-new table: every writer of the
# referenced table then queues behind the DDL.  So a CREATE TABLE carrying a
# foreign key is lock-taking DDL on an existing (usually hot) table.
_CREATE_TABLE_RE = re.compile(r"op\.create_table\(|\bCREATE\s+TABLE\b", re.I)
_FOREIGN_KEY_RE = re.compile(r"\bForeignKey(?:Constraint)?\(|\bREFERENCES\b", re.I)


def _code_only(source: str) -> str:
    """*source* with its comments and bare string statements blanked out.

    Both halves of the lock-safety guard read code, never prose: a comment
    explaining why a migration needs no ``lock_timeout`` must not be what
    satisfies the check, and a docstring describing an ``ALTER TABLE`` the
    migration does not run must not trip it.  Offsets are preserved (blanked
    characters become spaces) so the regexes see the code exactly as written.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    docstring_starts = {
        (node.value.lineno, node.value.col_offset)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    rows = [list(line) for line in source.splitlines(keepends=True)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        is_prose = token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING and token.start in docstring_starts
        )
        if not is_prose:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row_number in range(start_row, end_row + 1):
            row = rows[row_number - 1]
            first = start_col if row_number == start_row else 0
            last = end_col if row_number == end_row else len(row)
            for col in range(first, min(last, len(row))):
                if row[col] != "\n":
                    row[col] = " "
    return "".join("".join(row) for row in rows)

# Migrations merged before the lock-safety rule became mandatory
# (2026-07-05).  Frozen baseline: entries may only be removed, never added.
# Machine-enforced by ``test_lock_safety_grandfather_list_only_shrinks``:
# every entry must exist on disk (deleting a migration file forces its entry
# out) AND the list may never exceed ``_LOCK_SAFETY_GRANDFATHER_MAX``
# (grandfathering a NEW migration fails the build).
#
# Emptied by the squash: every grandfathered migration was deleted when
# the history was collapsed into a single baseline, so no legacy entries
# remain.  The list must stay empty — a new migration with lock-taking DDL is
# fixed with the canonical recipe (``app.core.migration_ddl``), never
# grandfathered.
_LOCK_SAFETY_GRANDFATHERED: frozenset[str] = frozenset()

# Frozen baseline size of the grandfather list.  The squash deleted every
# grandfathered migration, so the list is now empty and frozen at 0: any
# addition fails the build — a NEW migration can never be hidden by
# grandfathering it.
_LOCK_SAFETY_GRANDFATHER_MAX = 0


def _emits_lock_taking_ddl(source: str) -> bool:
    """Heuristic: does this migration emit DDL that takes contended locks?

    Raw ``ALTER TABLE`` / ``DROP TABLE`` / ``ALTER TYPE`` SQL, the
    lock-taking Alembic ops, plain (non-CONCURRENTLY) index DDL and a
    ``CREATE TABLE`` carrying a foreign key all count.  Alembic index ops are
    exempt when the file opts into ``postgresql_concurrently=True`` — the
    concurrent build is the lock-safe idiom and must NOT run under a
    ``lock_timeout``.  Comments and docstrings are not read.
    """
    code = _code_only(source)
    if _LOCK_TAKING_RE.search(code) or _PLAIN_INDEX_RE.search(code):
        return True
    if _CREATE_TABLE_RE.search(code) and _FOREIGN_KEY_RE.search(code):
        return True
    return bool(
        _ALEMBIC_INDEX_OP_RE.search(code)
        and not _ALEMBIC_CONCURRENT_KWARG_RE.search(code)
    )


def _sets_lock_timeout(source: str) -> bool:
    """Does the migration's CODE set the ``lock_timeout`` belt?

    The canonical helpers (``set_lock_timeout`` / ``terminate_lock_holders``
    are called, so their names are in the code) or an explicit
    ``SET LOCAL lock_timeout`` statement both count; a comment or a docstring
    naming the belt does not.
    """
    return "lock_timeout" in _code_only(source).lower()


def test_lock_taking_ddl_sets_lock_timeout():
    """Every NEW migration with lock-taking DDL must set a lock_timeout belt.

    Compliance = the file's CODE references ``lock_timeout`` — either through
    the canonical helpers (``from app.core.migration_ddl import
    set_lock_timeout, terminate_lock_holders``) or an explicit
    ``SET LOCAL lock_timeout``.  Presence-based on purpose: the goal is to
    make it impossible to merge a migration whose author never *thought*
    about lock contention.  A comment explaining why the belt is not needed
    does not count (three migrations passed that way).
    """
    offenders: list[str] = []
    for f in sorted(VERSIONS_DIR.glob("*.py")):
        if (
            f.name.startswith("__")
            or f.name in _LOCK_SAFETY_GRANDFATHERED
            or f.name in _BASELINE_REVISIONS
        ):
            continue
        source = f.read_text(encoding="utf-8")
        if _emits_lock_taking_ddl(source) and not _sets_lock_timeout(source):
            offenders.append(f.name)

    assert not offenders, (
        "Migration(s) emit lock-taking DDL without a lock_timeout safety "
        "belt (this exact gap took production down on 2026-07-05):\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n\nFix: follow the canonical recipe from dev-docs/reference/migrations-safety.md:\n"
        "  1. idempotency probe + early return\n"
        "  2. terminate_lock_holders(conn, <table>)   # app.core.migration_ddl\n"
        "  3. set_lock_timeout(conn)                  # app.core.migration_ddl\n"
        "  4. guarded DDL (IF NOT EXISTS / IF EXISTS)\n"
        "(An index: create_index_in_place() inside an autocommit_block sets "
        "the belt itself.)\n"
        "Do NOT add new entries to the grandfather list."
    )


# ---------------------------------------------------------------------------
# No concurrent index DDL in a migration body
#
# A migration body runs at startup, under the migration advisory lock, while
# the previous pods still serve. ``CREATE/DROP INDEX CONCURRENTLY`` waits for
# every transaction holding an older snapshot, in the whole database: the
# sibling workers queued on that very lock, and any long-lived session of the
# old pods. It may never finish, and the rollout hangs with it (the
# ``u5715sig01`` incident, 2026-10-12: 23 minutes, the worker waiting for the
# schema). The startup bootstrap learned it first
# (``app/core/migrations.py``): plain, guarded index DDL under the canonical
# recipe. These migrations predate the rule; the list may only shrink.
# ---------------------------------------------------------------------------

_CONCURRENT_INDEX_RE = re.compile(r"\bCONCURRENTLY\b|postgresql_concurrently\s*=\s*True", re.I)

_CONCURRENT_INDEX_GRANDFATHERED: frozenset[str] = frozenset({
    "a1paus3198_add_assignment_pause_provenance.py",
    "a2a6delegidx_index_sub_agent_parent_run.py",
    "a4732signin1_add_audit_log_sign_in_index.py",
    "a7f3run0001_add_runners.py",
    "a7f3run0002_add_runner_machine_id.py",
    "b2r7rep2720_add_reports.py",
    "c2f7sk1ll2628_add_skill_private_source_columns.py",
    "d1f9flw1909_add_flows.py",
    "d4e5ushr1881_add_per_user_sharing_columns.py",
    "e2232seedslug1_add_builtin_slug_to_datasets_and_evaluations.py",
    "e2c4flwrun2_add_flow_runs.py",
    "e3a9shr0439_add_conversation_sharing.py",
    "e3t9tpl2636_add_template_containers.py",
    "e5f7flwevt1_add_flow_events.py",
    "f1fail3128_add_failure_cause_columns.py",
    "f1sha2dedup01_add_file_attachment_sha256.py",
    "f2runix5258_index_file_attachments_assignment_run_id.py",
    "f3nova00rule_add_nova_rules.py",
    "f7p1res2637_add_presentations.py",
    "g1novasrc00_add_nova_rule_sources.py",
    "i7con1939005_investigation_continuation.py",
    "j4p3358collab_add_presentation_collaboration.py",
    "k5p3358studio_add_conversation_presentation_id.py",
    "m1mem516001_add_flow_events_memory_id_index.py",
    "m5688sup01_memory_supersession.py",
    "o2oapi00001_add_integration_openapi_specs.py",
    "r1rxn5511001_reaction_feedback.py",
    "r4244gaveup01_add_runner_command_given_up_at.py",
    "r5140relay01_add_relays.py",
    "s5459swi001_add_scheduled_work_items.py",
    "w2wsp3314002_workspace_conversations.py",
    "w3wsp3314003_workspace_tool_scope.py",
    "w4wsp3314004_workspace_shared_identity.py",
})
_CONCURRENT_INDEX_GRANDFATHER_MAX = 33


def test_no_new_migration_builds_an_index_concurrently():
    """A NEW migration never runs ``CREATE/DROP INDEX CONCURRENTLY``.

    Its body runs under the migration advisory lock during a rolling deploy;
    a concurrent build there waits on transactions that cannot end, and the
    rollout hangs. Build the index in place instead - ``create_index_in_place``
    inside an ``autocommit_block`` (``DROP INDEX IF EXISTS`` behind the
    canonical recipe) - or leave it out.
    Comments and docstrings are not read.
    """
    offenders = [
        f.name
        for f in sorted(VERSIONS_DIR.glob("*.py"))
        if not f.name.startswith("__")
        and f.name not in _CONCURRENT_INDEX_GRANDFATHERED
        and f.name not in _BASELINE_REVISIONS
        and _CONCURRENT_INDEX_RE.search(_code_only(f.read_text(encoding="utf-8")))
    ]
    assert not offenders, (
        "Migration(s) build or drop an index CONCURRENTLY - this hung a production "
        "rollout on 2026-10-12 (u5715sig01):\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n\nA migration body runs under the migration advisory lock while the old "
        "pods still serve; a concurrent build waits for every older transaction in "
        "the database and may never finish. Build it in place with create_index_in_place() "
        "inside op.get_context().autocommit_block(), drop it with DROP INDEX IF EXISTS "
        "behind terminate_lock_holders + set_lock_timeout (app.core.migration_ddl). Do "
        "NOT add the file to the grandfather list."
    )


def test_the_concurrent_index_grandfather_list_only_shrinks():
    existing = {f.name for f in VERSIONS_DIR.glob("*.py")}
    stale = sorted(_CONCURRENT_INDEX_GRANDFATHERED - existing)
    assert not stale, f"Grandfathered migrations that no longer exist (remove them): {stale}"
    assert len(_CONCURRENT_INDEX_GRANDFATHERED) <= _CONCURRENT_INDEX_GRANDFATHER_MAX, (
        "The concurrent-index grandfather list grew: fix the new migration instead. "
        "When removing entries, lower _CONCURRENT_INDEX_GRANDFATHER_MAX to match."
    )


def test_the_concurrent_index_tripwire_reads_code_only():
    assert _CONCURRENT_INDEX_RE.search(_code_only('op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c)")'))
    assert _CONCURRENT_INDEX_RE.search(_code_only("op.create_index('i', 't', ['c'], postgresql_concurrently=True)"))
    assert not _CONCURRENT_INDEX_RE.search(_code_only('"""Never CONCURRENTLY here."""\n# nor CONCURRENTLY\nx = 1'))
    assert not _CONCURRENT_INDEX_RE.search(_code_only('op.execute("CREATE INDEX IF NOT EXISTS i ON t (c)")'))


def test_lock_safety_grandfather_list_only_shrinks():
    """The grandfather list may only ever shrink. Two enforcement layers:

    1. Every entry must exist on disk — deleting a migration file forces
       its grandfather entry to be removed.
    2. The list may never exceed its frozen baseline size
       (``_LOCK_SAFETY_GRANDFATHER_MAX``) — a NEW migration cannot be
       hidden by adding it to the list, because any addition without at
       least a matching removal fails the build.
    """
    existing = {f.name for f in VERSIONS_DIR.glob("*.py")}
    stale = sorted(_LOCK_SAFETY_GRANDFATHERED - existing)
    assert not stale, (
        "Grandfather entries with no matching migration file (remove them "
        "from _LOCK_SAFETY_GRANDFATHERED):\n"
        + "\n".join(f"  - {s}" for s in stale)
    )
    assert len(_LOCK_SAFETY_GRANDFATHERED) <= _LOCK_SAFETY_GRANDFATHER_MAX, (
        f"The lock-safety grandfather list grew "
        f"({len(_LOCK_SAFETY_GRANDFATHERED)} > "
        f"{_LOCK_SAFETY_GRANDFATHER_MAX}). It is frozen at its 2026-07-05 "
        "baseline — fix the new migration with the canonical recipe from "
        "app.core.migration_ddl instead of grandfathering it. When removing "
        "entries, lower _LOCK_SAFETY_GRANDFATHER_MAX to match."
    )


def test_emits_lock_taking_ddl_heuristic():
    """Pin the heuristic's classification of the important DDL shapes."""
    # Lock-taking: raw SQL and Alembic ops.
    assert _emits_lock_taking_ddl('op.execute("ALTER TABLE t ADD COLUMN c TEXT")')
    assert _emits_lock_taking_ddl("op.add_column('t', sa.Column('c', sa.Text()))")
    assert _emits_lock_taking_ddl('op.execute("ALTER TYPE e ADD VALUE \'v\'")')
    # Plain index DDL is lock-taking; CONCURRENTLY is exempt.
    assert _emits_lock_taking_ddl('op.execute("CREATE INDEX IF NOT EXISTS i ON t (c)")')
    assert _emits_lock_taking_ddl('op.execute("DROP INDEX IF EXISTS i")')
    assert not _emits_lock_taking_ddl(
        'op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c)")'
    )
    assert not _emits_lock_taking_ddl(
        'op.execute("DROP INDEX CONCURRENTLY IF EXISTS i")'
    )
    # Alembic index ops: in-place builds are lock-taking, the
    # postgresql_concurrently=True variant is exempt.
    assert _emits_lock_taking_ddl("op.create_index('i', 't', ['c'])")
    assert _emits_lock_taking_ddl("op.drop_index('i', table_name='t')")
    assert not _emits_lock_taking_ddl(
        "op.create_index('i', 't', ['c'], postgresql_concurrently=True)"
    )
    # Data-only migrations take no contended lock.
    assert not _emits_lock_taking_ddl('op.execute("UPDATE t SET c = 1")')
    assert not _emits_lock_taking_ddl("op.bulk_insert(table, rows)")
    # A foreign key locks the table it references, inline in a new table too.
    assert _emits_lock_taking_ddl(
        "op.create_table('t', sa.Column('user_id', sa.Uuid(), "
        "sa.ForeignKey('users.id')))"
    )
    assert _emits_lock_taking_ddl(
        'op.execute("CREATE TABLE IF NOT EXISTS t (u UUID REFERENCES users(id))")'
    )
    assert not _emits_lock_taking_ddl(
        "op.create_table('t', sa.Column('id', sa.Uuid(), primary_key=True))"
    )
    # Prose is not DDL.
    assert not _emits_lock_taking_ddl("# no ALTER TABLE here\nop.bulk_insert(t, rows)")
    assert not _emits_lock_taking_ddl('"""Unlike an ALTER TABLE, ..."""\nx = 1')


def test_only_code_satisfies_the_lock_timeout_belt():
    """A comment or a docstring naming the belt is not the belt."""
    assert _sets_lock_timeout("set_lock_timeout(conn)")
    assert _sets_lock_timeout('op.execute("SET LOCAL lock_timeout = \'5s\'")')
    assert not _sets_lock_timeout(
        "# No `terminate_lock_holders` / `set_lock_timeout` here\nop.create_table('t')"
    )
    assert not _sets_lock_timeout('"""Built outside the lock_timeout belt."""\nx = 1')
