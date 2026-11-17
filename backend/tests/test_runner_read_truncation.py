"""A runner read that stopped before the end of the file says so.

Pure tests (no database, no Redis) over the ``read_file`` tool result: a read
the runner reports as cut (``truncated``) leads with how much was read and how
to get the rest; a runner from before that field, which cannot say, gets a
"may be longer" line when it returned exactly the size it was asked for; a read
that fits is returned as before.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.config import settings
from app.models.runner import Runner
from app.services.builtin_tools import computer_use
from app.services.builtin_tools.computer_use import build_computer_use_tools
from app.services.runners import RUNNER_DEFAULT_READ_BYTES, RUNNER_INLINE_TEXT_BYTES
from app.tools.base import ToolCall

LIMIT = 25_000_000


def _runner(owner: uuid.UUID) -> Runner:
    return Runner(
        id=uuid.uuid4(),
        user_id=owner,
        name="build-box",
        platform="linux",
        key_prefix="run-xxxxxxxx",
        key_hash="hash",
        permission_mode="restricted",
        capabilities=["file_ops"],
        labels=[],
        is_active=True,
        status="online",
        last_seen_at=datetime.now(UTC) - timedelta(seconds=5),
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    """What the tool reads from the database: the runner, then the stored file."""

    def __init__(self, runner, attachment):
        self._runner = runner
        self._attachment = attachment

    async def execute(self, *_a, **_k):
        return _Rows([self._runner])

    async def get(self, *_a, **_k):
        return self._attachment

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _read(monkeypatch, result: dict, *, stored_bytes: int | None = None, limit: int = LIMIT, **arguments) -> str:
    """The ``read_file`` tool result for a runner that answered ``result``.

    ``stored_bytes`` makes it a file result (the runner uploaded the bytes).
    """
    monkeypatch.setattr(settings, "runner_max_result_bytes", limit)
    owner = uuid.uuid4()
    runner = _runner(owner)
    attachment = None
    if stored_bytes is not None:
        attachment = SimpleNamespace(
            id=uuid.uuid4(),
            filename="app.log",
            content_type="application/octet-stream",
            size=stored_bytes,
            s3_object_name="runner-results/app.log",
        )
    monkeypatch.setattr(computer_use, "async_session", lambda: _Session(runner, attachment))
    monkeypatch.setattr(computer_use, "authorize_command", lambda *_a, **_k: None)
    monkeypatch.setattr(computer_use, "command_requires_approval", lambda *_a, **_k: False)

    async def _dispatch(**_kwargs):
        return "cmd-1", "pending"

    async def _await_result(_command_id, timeout=None, *, not_after=None):
        return {
            "id": "cmd-1",
            "status": "completed",
            "result": result,
            "error": None,
            "result_file_id": str(attachment.id) if attachment is not None else None,
            "kind": "read_file",
        }

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    monkeypatch.setattr(computer_use, "await_result", _await_result)
    _, executor = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        conversation_id=str(uuid.uuid4()),
    )
    call = ToolCall(id="tc-1", name="read_file", arguments={"path": "/var/log/app.log", **arguments})
    return asyncio.run(executor.execute_tool(call)).content


def test_a_read_the_runner_reports_cut_says_how_much_and_how_to_read_more(monkeypatch):
    content = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "content": "hello", "size": 5, "truncated": True},
        max_bytes=5,
    )
    assert content.startswith("Only the first 5 bytes were read: the file is longer")
    assert f"a larger max_bytes, up to {LIMIT}" in content
    assert content.endswith("\n\nhello")


def test_a_read_that_fits_is_returned_as_before(monkeypatch):
    content = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "content": "hello world", "size": 11, "truncated": False},
    )
    assert content == "hello world"


def test_an_empty_file_is_still_an_empty_file(monkeypatch):
    content = _read(monkeypatch, {"path": "/var/log/app.log", "content": "", "size": 0, "truncated": False})
    assert content == "(empty file)"


def test_a_file_cut_at_the_platform_limit_says_to_read_the_rest_another_way(monkeypatch):
    content = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "size": LIMIT, "encoding": "binary", "truncated": True},
        stored_bytes=LIMIT,
        max_bytes=100_000_000,
    )
    assert content.startswith(f"Only the first {LIMIT} bytes were read")
    assert f"{LIMIT} bytes is the largest file a runner may return (RUNNER_MAX_RESULT_BYTES)" in content
    assert "larger max_bytes" not in content
    assert f"File retrieved from 'build-box' ({LIMIT} bytes)" in content


def test_a_file_result_that_fits_is_returned_as_before(monkeypatch):
    content = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "size": 400_000, "encoding": "binary", "truncated": False},
        stored_bytes=400_000,
    )
    assert content.startswith("File retrieved from 'build-box' (400000 bytes).")


def test_an_older_runner_that_filled_the_size_asked_may_have_cut_the_file(monkeypatch):
    # No ``truncated`` field: the runner predates it. With no max_bytes the
    # runner was asked for its own default, under the platform limit.
    content = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "size": RUNNER_DEFAULT_READ_BYTES, "encoding": "binary"},
        stored_bytes=RUNNER_DEFAULT_READ_BYTES,
    )
    assert content.startswith(
        f"The read stopped at the {RUNNER_DEFAULT_READ_BYTES} bytes it was asked for: the file may be longer"
    )
    assert f"a larger max_bytes, up to {LIMIT}" in content

    asked = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "content": "x" * 100, "size": 100},
        max_bytes=100,
    )
    assert asked.startswith("The read stopped at the 100 bytes it was asked for")


def test_an_older_runner_that_read_less_than_asked_says_nothing_more(monkeypatch):
    content = _read(monkeypatch, {"path": "/var/log/app.log", "content": "hello world", "size": 11})
    assert content == "hello world"


def test_under_a_limit_below_the_inline_size_a_text_read_can_ask_for_the_inline_size(monkeypatch):
    # A runner that honours ``max_inline_bytes`` says so in every inline result
    # (``inline_text_limit``), whether or not the read needed the allowance, and
    # reads text up to the inline size under a smaller limit: a cut text
    # read can still ask for that much, and one past the limit is not "the
    # largest file a runner may return". A read that asked for no more than the
    # limit carries no ``max_inline_bytes`` (none was sent) and is no older runner.
    small = 1000
    for size, applied in ((500, None), (small, None), (2000, 2000)):
        result = {
            "path": "/var/log/app.log",
            "content": "x" * size,
            "size": size,
            "truncated": True,
            "inline_text_limit": RUNNER_INLINE_TEXT_BYTES,
        }
        if applied:
            result["max_inline_bytes"] = applied
        content = _read(monkeypatch, result, limit=small, max_bytes=size)
        assert f"a larger max_bytes, up to {RUNNER_INLINE_TEXT_BYTES}" in content, size
        assert "largest file" not in content, size
        assert "shell command" not in content, size
    # A runner from before sends neither field and reads at most the limit, as on main.
    older = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "content": "x" * small, "size": small, "truncated": True},
        limit=small,
        max_bytes=10**6,
    )
    assert f"{small} bytes is the largest file a runner may return" in older
    older = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "content": "x" * 500, "size": 500, "truncated": True},
        limit=small,
        max_bytes=500,
    )
    assert f"a larger max_bytes, up to {small}" in older
    # At the inline size, the rest can only come back as a file, which the limit refuses.
    full = _read(
        monkeypatch,
        {
            "path": "/var/log/app.log",
            "content": "x" * RUNNER_INLINE_TEXT_BYTES,
            "size": RUNNER_INLINE_TEXT_BYTES,
            "truncated": True,
            "max_inline_bytes": RUNNER_INLINE_TEXT_BYTES,
            "inline_text_limit": RUNNER_INLINE_TEXT_BYTES,
        },
        limit=small,
    )
    assert f"{RUNNER_INLINE_TEXT_BYTES} bytes is the most text a runner returns inline" in full
    assert f"RUNNER_MAX_RESULT_BYTES ({small} bytes)" in full
    assert "larger max_bytes" not in full
    # A file result is still bounded by the limit itself.
    cut_file = _read(
        monkeypatch,
        {"path": "/var/log/app.log", "size": 500, "encoding": "binary", "truncated": True},
        stored_bytes=500,
        limit=small,
        max_bytes=500,
    )
    assert f"a larger max_bytes, up to {small}" in cut_file


def test_a_read_cut_to_nothing_is_not_called_an_empty_file(monkeypatch):
    content = _read(monkeypatch, {"path": "/var/log/app.log", "content": "", "size": 0, "truncated": True})
    assert content.startswith("Only the first 0 bytes were read")
    assert content.endswith("(nothing read)")
    assert "(empty file)" not in content
