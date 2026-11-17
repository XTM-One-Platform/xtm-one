"""The runner receives its allowed folders and a capped read size.

The backend checks a file command's path as written (``authorize_command``);
only the runner can resolve links, so a restricted runner with an allowed-paths
list gets those folders as ``allowed_roots`` when the command is delivered
(with ``strict_confinement`` only when the operator opted in), and a read never
asks for more than a positive result upload limit, never below the text the
runner returns inline.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import security_audit
from app.core.config import Settings, settings
from app.models.runner import PERMISSION_MODE_FULL_CONTROL
from app.services import runner_transport
from app.services.runners import (
    ALLOWED_ROOTS_KEY,
    MAX_INLINE_BYTES_KEY,
    RUNNER_DEFAULT_READ_BYTES,
    RUNNER_INLINE_TEXT_BYTES,
    STRICT_CONFINEMENT_KEY,
    delivery_payload,
)


def _runner(*, mode: str = "restricted", allowed_paths=None, capabilities=("file_ops",)):
    return SimpleNamespace(
        permission_mode=mode,
        allowed_paths=allowed_paths,
        capabilities=list(capabilities),
        is_active=True,
        platform="linux",
    )


@pytest.mark.parametrize("kind", ["read_file", "write_file", "list_dir"])
def test_a_restricted_runner_receives_its_allowed_folders(kind):
    runner = _runner(allowed_paths=["/srv/data", "  ", "/srv/reports "])
    out = delivery_payload(runner, kind, {"path": "/srv/data/a.txt"})
    # As configured: the check of the path as written compares them unstripped.
    assert out[ALLOWED_ROOTS_KEY] == ["/srv/data", "/srv/reports "]
    assert out["path"] == "/srv/data/a.txt"


def test_a_supplied_value_is_replaced_never_trusted():
    runner = _runner(allowed_paths=["/srv/data"])
    out = delivery_payload(runner, "list_dir", {"path": "/", ALLOWED_ROOTS_KEY: ["/"]})
    assert out[ALLOWED_ROOTS_KEY] == ["/srv/data"]


@pytest.mark.parametrize(
    "runner",
    [
        _runner(mode=PERMISSION_MODE_FULL_CONTROL, allowed_paths=["/srv/data"]),
        _runner(allowed_paths=None),
        _runner(allowed_paths=[]),
        None,
    ],
)
def test_no_confinement_means_no_roots_even_a_supplied_one(runner):
    out = delivery_payload(runner, "write_file", {"path": "/x", ALLOWED_ROOTS_KEY: ["/"]})
    assert ALLOWED_ROOTS_KEY not in out


def test_other_command_kinds_are_delivered_as_asked():
    payload = {"command": "ls", ALLOWED_ROOTS_KEY: ["/"]}
    assert delivery_payload(_runner(allowed_paths=["/srv"]), "run_shell", payload) == payload


def test_the_stored_payload_is_not_modified():
    payload = {"path": "/srv/data/a.txt"}
    delivery_payload(_runner(allowed_paths=["/srv/data"]), "read_file", payload)
    assert payload == {"path": "/srv/data/a.txt"}


def test_a_read_never_asks_for_more_than_the_result_limit(monkeypatch):
    monkeypatch.setattr(settings, "runner_max_result_bytes", 1_000_000)
    runner = _runner()
    assert delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": 10**12})["max_bytes"] == 1_000_000
    # A size up to the limit is delivered as asked, zero included.
    for size in (0, 10, 1_000_000):
        assert delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": size})["max_bytes"] == size
    # A size the runner would not read becomes its own default, under the limit.
    for invalid in (None, -5, True, "big", 12.5):
        payload = {"path": "/a"} if invalid is None else {"path": "/a", "max_bytes": invalid}
        assert delivery_payload(runner, "read_file", payload)["max_bytes"] == 1_000_000


def test_a_small_limit_still_caps_what_comes_back_as_a_file(monkeypatch):
    # Under a limit below the inline text size, a file result is cut to the
    # limit, never refused at upload: ``max_bytes`` stays the limit, as on main,
    # so a runner that knows nothing more reads exactly what it did there.
    monkeypatch.setattr(settings, "runner_max_result_bytes", 1000)
    runner = _runner()
    for size in (10**12, RUNNER_INLINE_TEXT_BYTES, 1001):
        assert delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": size})["max_bytes"] == 1000
    assert delivery_payload(runner, "read_file", {"path": "/a"})["max_bytes"] == 1000


@pytest.mark.parametrize("limit", [1000, RUNNER_INLINE_TEXT_BYTES - 1])
def test_a_small_limit_never_cuts_what_the_runner_returns_inline(monkeypatch, limit):
    # Text up to RUNNER_INLINE_TEXT_BYTES comes back inline, where the result
    # limit never applied: a runner that knows it may read that much for an
    # inline result (``max_inline_bytes``, optional - an older runner ignores
    # it and reads ``max_bytes``).
    monkeypatch.setattr(settings, "runner_max_result_bytes", limit)
    runner = _runner()
    for size, inline in (
        (10**12, RUNNER_INLINE_TEXT_BYTES),
        (RUNNER_INLINE_TEXT_BYTES, RUNNER_INLINE_TEXT_BYTES),
        (limit + 1, limit + 1),
    ):
        out = delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": size})
        assert (out["max_bytes"], out[MAX_INLINE_BYTES_KEY]) == (limit, inline)
    assert delivery_payload(runner, "read_file", {"path": "/a"})[MAX_INLINE_BYTES_KEY] == RUNNER_INLINE_TEXT_BYTES
    # Nothing to add where the limit already covers the read, or with no limit.
    for size in (0, 10, limit):
        assert MAX_INLINE_BYTES_KEY not in delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": size})
    monkeypatch.setattr(settings, "runner_max_result_bytes", RUNNER_INLINE_TEXT_BYTES)
    assert MAX_INLINE_BYTES_KEY not in delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": 10**12})
    monkeypatch.setattr(settings, "runner_max_result_bytes", 0)
    assert MAX_INLINE_BYTES_KEY not in delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": 10**12})
    # A value an agent put in the payload is never trusted.
    monkeypatch.setattr(settings, "runner_max_result_bytes", 25_000_000)
    supplied = {"path": "/a", "max_bytes": 10, MAX_INLINE_BYTES_KEY: 10**12}
    assert MAX_INLINE_BYTES_KEY not in delivery_payload(runner, "read_file", supplied)
    assert MAX_INLINE_BYTES_KEY not in delivery_payload(runner, "write_file", supplied)


def test_the_read_sizes_mirror_the_runner():
    """The two sizes delivery reasons with are the runner's own constants."""
    source = (Path(__file__).resolve().parents[2] / "apps" / "runner" / "src" / "capabilities" / "files.rs").read_text(encoding="utf-8")

    def constant(name: str) -> int:
        match = re.search(rf"const {name}: \w+ = ([\d_]+);", source)
        assert match, name
        return int(match.group(1).replace("_", ""))

    assert constant("INLINE_TEXT_LIMIT") == RUNNER_INLINE_TEXT_BYTES
    assert constant("DEFAULT_READ_LIMIT") == RUNNER_DEFAULT_READ_BYTES


def test_a_read_without_a_size_keeps_the_runner_default_under_a_large_limit(monkeypatch):
    monkeypatch.setattr(settings, "runner_max_result_bytes", 25_000_000)
    out = delivery_payload(_runner(), "read_file", {"path": "/a"})
    assert out["max_bytes"] == RUNNER_DEFAULT_READ_BYTES


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_of_zero_or_less_keeps_its_meaning(monkeypatch, limit):
    # As before this change: no bound at delivery (the upload refuses every
    # file result under such a limit); the security audit reports it.
    monkeypatch.setattr(settings, "runner_max_result_bytes", limit)
    runner = _runner()
    for size in (10**12, 10, 0):
        assert delivery_payload(runner, "read_file", {"path": "/a", "max_bytes": size})["max_bytes"] == size
    assert delivery_payload(runner, "read_file", {"path": "/a"})["max_bytes"] == RUNNER_DEFAULT_READ_BYTES


def test_strict_confinement_is_an_opt_in_never_a_default():
    assert type(settings).model_fields["runner_strict_path_confinement"].default is False


def test_a_limit_of_zero_or_less_is_said_at_startup(caplog):
    with caplog.at_level("WARNING", logger="app.core.config"):
        Settings(runner_max_result_bytes=0)
    assert any("RUNNER_MAX_RESULT_BYTES=0" in record.getMessage() for record in caplog.records)


class _AllowListDb:
    """How many runners are active, and ``(allowed_paths, capabilities)`` of the active restricted ones."""

    def __init__(self, *runners, active: int | None = None):
        self._runners = list(runners)
        self._active = len(self._runners) if active is None else active
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return SimpleNamespace(scalar=lambda: self._active, all=lambda: list(self._runners))


FILE_OPS = ["file_ops"]


def _ids(findings: list[security_audit.AuditFinding]) -> list[str | None]:
    return [finding.id for finding in findings]


@pytest.mark.asyncio
async def test_the_security_audit_reports_the_link_default_while_runners_have_allowed_folders(
    monkeypatch,
):
    monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", False)
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", 25_000_000)
    findings: list[security_audit.AuditFinding] = []
    db = _AllowListDb(
        (["/srv/data"], FILE_OPS),
        (["C:\\work"], ["run_shell", "file_ops"]),
        (None, FILE_OPS),
        ([], FILE_OPS),
        # No file operation can run there, or no folder to hold one to.
        (["/srv/data"], ["run_shell"]),
        (["  "], FILE_OPS),
    )
    await security_audit._check_runners(db, findings)
    assert _ids(findings) == ["tools.runner_reads_bounded", "tools.runner_path_links"]
    finding = findings[1]
    assert (finding.category, finding.severity) == ("tools", "warning")
    assert "(2 runner(s))" in finding.title
    assert "RUNNER_STRICT_PATH_CONFINEMENT=true" in (finding.recommendation or "")
    assert finding.control == "tools.runner_strict_path_confinement"


@pytest.mark.asyncio
async def test_the_security_audit_reports_strict_confinement_and_bounded_reads_as_passes(monkeypatch):
    # No silent pass: what is enforced while runners use it says so.
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", 25_000_000)
    monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", True)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_runners(
        _AllowListDb((["/srv/data"], FILE_OPS), (None, FILE_OPS)), findings
    )
    assert _ids(findings) == ["tools.runner_reads_bounded", "tools.runner_path_confinement_strict"]
    assert {finding.severity for finding in findings} == {"pass"}
    assert "(1 runner(s))" in findings[1].title


@pytest.mark.asyncio
async def test_the_security_audit_says_nothing_of_confinement_without_allowed_folders(monkeypatch):
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", 25_000_000)
    for strict in (False, True):
        monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", strict)
        findings: list[security_audit.AuditFinding] = []
        await security_audit._check_runners(_AllowListDb((None, FILE_OPS), ([], FILE_OPS)), findings)
        assert _ids(findings) == ["tools.runner_reads_bounded"]


@pytest.mark.asyncio
async def test_the_security_audit_is_silent_without_an_active_runner(monkeypatch):
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", 25_000_000)
    monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", False)
    findings: list[security_audit.AuditFinding] = []
    db = _AllowListDb(active=0)
    await security_audit._check_runners(db, findings)
    assert findings == []
    assert db.queries == 1


@pytest.mark.parametrize("limit", [0, -1, -5])
@pytest.mark.asyncio
async def test_a_limit_of_zero_or_less_refuses_a_file_result_without_reading_it_all(
    monkeypatch, limit
):
    from fastapi import HTTPException

    from app.api.v1 import runners as runners_api
    from app.services import runner_transport

    recorded: list[str] = []

    async def _submit(_command_id, *, status, **_kwargs):
        recorded.append(status)
        return True

    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", limit)
    monkeypatch.setattr(runner_transport, "submit_result", _submit)
    runner_id = uuid.uuid4()
    command = SimpleNamespace(id=uuid.uuid4(), runner_id=runner_id, status="dispatched")
    asked: list[int] = []

    class _Upload:
        filename = "a.bin"
        content_type = "application/octet-stream"

        async def read(self, size: int = -1) -> bytes:
            asked.append(size)
            return b"x" * (10 if size < 0 else min(size, 10))

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: command)

    with pytest.raises(HTTPException) as refused:
        await runners_api._accept_command_result(
            command.id,
            '{"status": "completed"}',
            _Upload(),
            SimpleNamespace(id=runner_id),
            _Db(),
        )
    assert refused.value.status_code == 413
    # Every file result is refused, as before, and never by reading it all;
    # the command ends as failed instead of waiting for a result.
    assert asked == [1]
    assert recorded == ["failed"]


@pytest.mark.parametrize(("limit", "stored"), [(0, True), (-1, False)])
@pytest.mark.asyncio
async def test_an_empty_file_result_fits_a_limit_of_zero_as_before(monkeypatch, limit, stored):
    # The check is "larger than the limit", unchanged: an empty upload fits a
    # limit of 0 (and reaches storage), never one below it.
    from fastapi import HTTPException

    from app.api.v1 import runners as runners_api
    from app.services import runner_transport
    from app.services.builtin_tools import _shared

    class _StoredError(Exception):
        pass

    async def _persist(**_kwargs):
        raise _StoredError

    async def _submit(_command_id, **_kwargs):
        return True

    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", limit)
    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    monkeypatch.setattr(runner_transport, "submit_result", _submit)
    runner_id = uuid.uuid4()
    command = SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=runner_id,
        status="dispatched",
        conversation_id=None,
        assignment_run_id=None,
        user_id=None,
    )

    class _Upload:
        filename = "empty.bin"
        content_type = "application/octet-stream"

        async def read(self, size: int = -1) -> bytes:
            return b""

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: command)

    expected = _StoredError if stored else HTTPException
    with pytest.raises(expected):
        await runners_api._accept_command_result(
            command.id,
            '{"status": "completed"}',
            _Upload(),
            SimpleNamespace(id=runner_id),
            _Db(),
        )


@pytest.mark.parametrize("limit", [0, -1])
@pytest.mark.parametrize(("active", "severity"), [(2, "warning"), (0, "info")])
@pytest.mark.asyncio
async def test_the_security_audit_reports_a_limit_of_zero_or_less(monkeypatch, limit, active, severity):
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", limit)
    monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", True)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_runners(_AllowListDb(active=active), findings)
    [finding] = findings
    assert finding.id == "tools.runner_reads_unbounded"
    # Off while runners use it is a warning; off with no runner is context.
    assert finding.severity == severity
    assert f"RUNNER_MAX_RESULT_BYTES is {limit}" in finding.description
    assert finding.control == "tools.runner_result_bound"


@pytest.mark.parametrize("limit", [1_000, RUNNER_INLINE_TEXT_BYTES, 25_000_000])
@pytest.mark.asyncio
async def test_the_security_audit_states_the_bound_a_read_really_has(monkeypatch, limit):
    # Text up to RUNNER_INLINE_TEXT_BYTES comes back inline and never meets the
    # limit, so under a smaller limit a read may ask for that much: the finding
    # states the bound a read really has, never the setting alone.
    monkeypatch.setattr(security_audit.settings, "runner_max_result_bytes", limit)
    monkeypatch.setattr(security_audit.settings, "runner_strict_path_confinement", True)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_runners(_AllowListDb(active=1), findings)
    [finding] = [f for f in findings if f.id == "tools.runner_reads_bounded"]
    assert f"RUNNER_MAX_RESULT_BYTES is {limit:,}" in finding.description
    if limit < RUNNER_INLINE_TEXT_BYTES:
        assert (
            f"never asks a runner for more than {RUNNER_INLINE_TEXT_BYTES:,} bytes"
            in finding.description
        )
    else:
        assert "never asks a runner for more," in finding.description
        assert "inline" not in finding.description


def test_the_runner_is_told_to_refuse_only_when_strict_is_opted_in(monkeypatch):
    runner = _runner(allowed_paths=["/srv/data"])
    # A value already in the payload is never trusted, either way.
    payload = {"path": "/srv/data/a.txt", STRICT_CONFINEMENT_KEY: True}
    monkeypatch.setattr(settings, "runner_strict_path_confinement", False)
    assert STRICT_CONFINEMENT_KEY not in delivery_payload(runner, "read_file", payload)
    monkeypatch.setattr(settings, "runner_strict_path_confinement", True)
    assert delivery_payload(runner, "read_file", payload)[STRICT_CONFINEMENT_KEY] is True
    # Without allowed folders too, full control included: the runner also
    # refuses what is not a regular file under it (a device, a pipe, a
    # socket, as the OS reports what it opened). No roots come with it.
    for unconfined in (
        _runner(allowed_paths=None),
        _runner(mode=PERMISSION_MODE_FULL_CONTROL, allowed_paths=["/srv/data"]),
        None,
    ):
        for kind in ("read_file", "write_file", "list_dir"):
            out = delivery_payload(unconfined, kind, payload)
            assert out[STRICT_CONFINEMENT_KEY] is True
            assert ALLOWED_ROOTS_KEY not in out
        monkeypatch.setattr(settings, "runner_strict_path_confinement", False)
        assert STRICT_CONFINEMENT_KEY not in delivery_payload(unconfined, "read_file", payload)
        monkeypatch.setattr(settings, "runner_strict_path_confinement", True)
    # Never on a command that is not a file operation.
    assert STRICT_CONFINEMENT_KEY not in delivery_payload(runner, "run_shell", {"command": "ls"})


@pytest.mark.asyncio
async def test_the_delivered_command_carries_the_runner_decisions():
    runner_id = uuid.uuid4()
    command = SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=runner_id,
        kind="read_file",
        payload={"path": "/srv/data/a.txt"},
        status="pending",
        dispatched_at=None,
    )
    runner = _runner(allowed_paths=["/srv/data"])

    class _Session:
        def __init__(self):
            self.commit = AsyncMock()
            self.rollback = AsyncMock()

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: command)

        async def get(self, _model, key, **_options):
            assert key == runner_id
            return runner

    delivered = await runner_transport.claim_next_command(_Session(), runner_id)

    assert delivered["payload"][ALLOWED_ROOTS_KEY] == ["/srv/data"]
    assert "max_bytes" in delivered["payload"]
    assert command.payload == {"path": "/srv/data/a.txt"}
    assert command.status == "dispatched"


def _queued(runner_id, kind: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=runner_id,
        kind=kind,
        payload=payload,
        status="pending",
        dispatched_at=None,
        error=None,
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_a_command_the_current_policy_forbids_is_cancelled_not_delivered(monkeypatch):
    """Re-authorized at delivery, like the approval route: a read queued before
    `file_ops` was removed is cancelled with the refusal, and the next command
    the policy still allows is delivered."""
    runner_id = uuid.uuid4()
    forbidden = _queued(runner_id, "read_file", {"path": "/srv/data/a.txt"})
    allowed = _queued(runner_id, "host_facts", {})
    queue = [forbidden, allowed]
    runner = _runner(allowed_paths=["/srv/data"], capabilities=())
    announced = []

    async def announce(command_id, status):
        announced.append((command_id, status))

    monkeypatch.setattr(runner_transport, "_emit_command_terminal", AsyncMock())
    monkeypatch.setattr(runner_transport, "_announce_command_terminal", announce)

    class _Session:
        def __init__(self):
            self.commit = AsyncMock()
            self.rollback = AsyncMock()

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: queue.pop(0) if queue else None)

        async def get(self, _model, key, **_options):
            return runner

    delivered = await runner_transport.claim_next_command(_Session(), runner_id)

    assert delivered["id"] == str(allowed.id)
    assert allowed.status == "dispatched"
    assert forbidden.status == "cancelled"
    assert "at delivery" in forbidden.error and "'file_ops' capability" in forbidden.error
    assert announced == [(forbidden.id, "cancelled")]


def test_the_read_file_schema_still_accepts_every_size():
    """The limit is applied at delivery: a call the schema accepted before is never refused."""
    from app.services.builtin_tools.computer_use import _tool_definitions
    from app.tools.agent_loop import validate_tool_arguments

    schema = next(tool for tool in _tool_definitions() if tool.name == "runner_read_file").input_schema
    for size in (0, 10**12):
        assert validate_tool_arguments("read_file", {"path": "/a", "max_bytes": size}, schema) is None
