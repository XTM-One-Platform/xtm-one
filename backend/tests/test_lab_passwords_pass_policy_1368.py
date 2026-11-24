"""Every user password the lab seeds passes the platform's own password policy (#1368).

``seed_org_syncs`` created its sync owner with a password that had no digit: ``POST /users`` answered
400 and the whole seed was skipped in every lab. This reads each ``*_PASSWORD`` string literal of
``lab.py`` (the users it creates on XTM One) and checks it against ``check_password_policy`` at the default
settings. The ``*_pr_head.py`` labs sign in to other products (OpenCTI, XTM Hub...), whose policies differ.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.security import check_password_policy

LAB = Path(__file__).resolve().parents[2] / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _password_literals() -> list[tuple[str, str]]:
    if not LAB.is_file():
        return []
    found: list[tuple[str, str]] = []
    for node in ast.parse(LAB.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_PASSWORD"):
                found.append((target.id, node.value.value))
    return found


def test_the_lab_declares_password_literals():
    names = {name for name, _ in _password_literals()}
    assert "ORG_SYNC_OWNER_PASSWORD" in names


@pytest.mark.parametrize(("name", "password"), _password_literals(), ids=lambda v: str(v)[:40])
def test_each_lab_password_passes_the_policy(name, password):
    check_password_policy(password)
