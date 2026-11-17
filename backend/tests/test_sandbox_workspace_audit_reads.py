"""A sandbox workspace read writes no audit row; every state change still does (#9743).

The per-file read handed to a tool (``workspace_read_for_tool``) was a quarter
of the audit log of a busy install and recorded no change. These tests pin
both halves: the read is silent, and the audit actions of the writes, the
commands, the services and the sweep are still emitted.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.services.sandbox_workspace import audit, ingest

APP = Path(__file__).resolve().parents[1] / "app"
SOURCES = [
    APP / "services" / "builtin_tools" / "sandbox_workspace.py",
    *sorted((APP / "services" / "sandbox_workspace").glob("*.py")),
]

# The state changes the log exists for: removing one of these is a regression.
STATE_CHANGE_ACTIONS = {
    "workspace_put",
    "workspace_move",
    "workspace_copy",
    "workspace_edit",
    "workspace_write",
    "materialize_repository",
    "checkout_rebase",
    "sandbox_run_command",
    "sandbox_service",
    "delete",
}


def _audited_actions() -> set[str]:
    """The literal first argument of every ``audit.record`` / ``audit.record_system`` call."""
    actions: set[str] = set()
    for source in SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            func = node.func
            if func.attr not in {"record", "record_system"}:
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "audit"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                actions.add(node.args[0].value)
    return actions


def test_no_sandbox_workspace_read_is_audited():
    actions = _audited_actions()
    reads = sorted(a for a in actions if "read" in a or a.endswith("_get") or a == "workspace_get")
    assert reads == [], f"a read changes no state and must not write an audit row: {reads}"


def test_every_state_change_is_still_audited():
    missing = sorted(STATE_CHANGE_ACTIONS - _audited_actions())
    assert missing == [], f"state changes no longer audited: {missing}"


class _Session:
    available = True
    unavailable_reason = "no workspace"
    workspace_id = uuid.uuid4()
    scope = SimpleNamespace(user_id=uuid.uuid4())

    async def settled(self) -> None:
        """No write in flight: a reader waits for its tool round's writes first (#11714)."""

    async def get_bytes(self, rel: str):
        data = b"hello"
        return SimpleNamespace(path=rel, size=len(data), sha256="x"), data


def test_a_file_read_for_a_tool_writes_no_audit_row(monkeypatch):
    calls: list[tuple] = []

    async def _record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(audit, "record", _record)
    monkeypatch.setattr(audit, "record_system", _record)

    reader = ingest.WorkspaceIngestor(_Session())
    rel, name, data = asyncio.run(reader.read_file("src/app.py", tool="create_github_commit"))

    assert (rel, name, data) == ("src/app.py", "app.py", b"hello")
    assert calls == []


def test_a_state_change_still_reaches_the_audit_log(monkeypatch):
    rows: list[dict] = []

    class _Db:
        async def get(self, _model, _id):
            return SimpleNamespace(id=_id)

        async def commit(self):
            return None

    class _Ctx:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, *_exc):
            return False

    async def _log_audit(_db, **kwargs):
        rows.append(kwargs)

    monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx())
    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)

    asyncio.run(
        audit.record("workspace_write", user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), summary="Created a.py")
    )

    assert [row["action"] for row in rows] == ["workspace_write"]
