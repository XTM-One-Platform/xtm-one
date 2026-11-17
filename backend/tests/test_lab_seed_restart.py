"""A lab restarted in place keeps the database its last run seeded and skips the seed (#12253).

lab.py is standard library only and starts the stores when imported, so its functions are compiled one by one from
its syntax tree (as ``test_lab_doc_draft.py`` does). The restart path must answer within the service's
``restart_target``: the seed (about 160 s) is skipped when the database it wrote is kept and the lab scripts and
switches that wrote it are unchanged; any other run drops ``pgdata`` before the stores start and seeds a fresh one.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

_NAMES = ("SEED_MARKER", "SEED_ENV_PREFIXES", "seed_fingerprint", "kept_seed", "seed_lab")


def _lab_namespace(calls: list[str], root: Path) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]
    namespace: dict = {
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "shutil": shutil,
        "Path": Path,
        "ROOT": root,
        "CHECKOUT": ROOT,
        "log": lambda _line: None,
        "seed_entities": lambda: calls.append("seed"),
        "restore_seed": lambda: calls.append("restore"),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _seeded_root(root: Path) -> None:
    (root / "pgdata").mkdir(parents=True)
    (root / "pgdata" / "PG_VERSION").write_text("17\n")
    (root / "seeded.json").write_text(json.dumps({"seeded": {}, "skipped": {}}))


def test_a_first_start_seeds_then_writes_its_marker(tmp_path: Path) -> None:
    calls: list[str] = []
    ns = _lab_namespace(calls, tmp_path)
    kept = ns["kept_seed"](tmp_path, "abc")
    assert kept is False
    ns["seed_lab"](kept, "abc")
    assert calls == ["seed"]
    assert (tmp_path / ns["SEED_MARKER"]).read_text() == "abc"


def test_a_restart_with_the_same_scripts_keeps_the_database_and_skips_the_seed(tmp_path: Path) -> None:
    calls: list[str] = []
    ns = _lab_namespace(calls, tmp_path)
    _seeded_root(tmp_path)
    (tmp_path / ns["SEED_MARKER"]).write_text("abc")
    kept = ns["kept_seed"](tmp_path, "abc")
    assert kept is True and (tmp_path / "pgdata" / "PG_VERSION").is_file()
    ns["seed_lab"](kept, "abc")
    assert calls == ["restore"]  # the in-memory rows only, never seed_entities


def test_a_changed_lab_script_or_a_cut_seed_drops_the_database_before_the_stores(tmp_path: Path) -> None:
    calls: list[str] = []
    ns = _lab_namespace(calls, tmp_path)
    _seeded_root(tmp_path)
    (tmp_path / ns["SEED_MARKER"]).write_text("old")
    assert ns["kept_seed"](tmp_path, "new") is False
    assert not (tmp_path / "pgdata").exists() and not (tmp_path / ns["SEED_MARKER"]).exists()
    # A seed cut half-way never wrote its marker: the half-seeded database is dropped too.
    _seeded_root(tmp_path)
    assert ns["kept_seed"](tmp_path, "new") is False and not (tmp_path / "pgdata").exists()


def test_the_fingerprint_follows_the_lab_scripts_and_the_seed_switches(tmp_path: Path) -> None:
    ns = _lab_namespace([], tmp_path)
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "lab.py").write_text("one")
    env = {"LAB_THEME": "dark", "PATH": "/bin"}
    first = ns["seed_fingerprint"](lab, env)
    assert ns["seed_fingerprint"](lab, {**env, "PATH": "/usr/bin"}) == first  # not a seed switch
    assert ns["seed_fingerprint"](lab, {**env, "LAB_THEME": "light"}) != first
    (lab / "seed_x.py").write_text("two")
    assert ns["seed_fingerprint"](lab, env) != first


def test_the_lab_drops_pgdata_only_through_kept_seed_and_before_its_stores() -> None:
    source = LAB.read_text(encoding="utf-8")
    main = source.split("\ndef main(", 1)[1]
    assert main.index("kept_seed(ROOT, fingerprint)") < main.index("start_stores()")
    assert "seed_lab(kept, fingerprint)" in main and "\n    seed_entities()\n" not in main
    tail = source.split('if __name__ == "__main__":', 1)[1]
    assert 'rmtree(ROOT / "pgdata"' not in tail
