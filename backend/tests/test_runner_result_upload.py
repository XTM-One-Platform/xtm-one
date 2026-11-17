"""The runner's result upload.

A runner uploads a command's result as a multipart form: the structured result
as the JSON ``meta`` field, a binary artifact as the ``file`` part. Declared as
a FastAPI ``Form`` field, ``meta`` was parsed under Starlette's default of
1 MiB per field, so a shell command's output - up to 1 MiB of stdout and 1 MiB
of stderr on the machine - was refused with ``Part exceeded maximum size of
1024KB.``, and the runner, which never sends a refused result again, left the
command waiting until its waiter gave up.

These tests post the form the way the runner builds it, through the real
route and parser: a ``meta`` up to the limit shared with the runner is
accepted, a file is held to its own limit and the body to the two, and a
result the platform refuses ends the command as failed with a reason the
agent reads.
"""

import json
import math
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_runner
from app.api.v1 import runners as runners_api
from app.core.database import get_db
from app.services import runner_transport
from app.services.builtin_tools import _shared
from app.services.runner_transport import RUNNER_MAX_RESULT_META_BYTES

RUNNER_SRC = Path(__file__).resolve().parents[2] / "apps" / "runner" / "src"
# Starlette's default bound on a form field, which refused the results.
FRAMEWORK_FIELD_LIMIT = 1024 * 1024
BOUNDARY = "xtm-one-runner-test-boundary"


def _multipart(meta: str | None, files: list[tuple[str, bytes]] = ()) -> bytes:
    """A result upload as the runner sends it: ``meta`` as a text field, each
    file as a part with a file name."""
    parts: list[bytes] = []
    if meta is not None:
        parts.append(
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="meta"\r\n\r\n'.encode()
            + meta.encode()
            + b"\r\n"
        )
    for name, content in files:
        parts.append(
            f"--{BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


SHELL_RESULT = {"exit_code": 0, "stdout": "", "stderr": ""}


def _meta_of_length(length: int, result: dict = SHELL_RESULT, field: str = "stdout") -> str:
    """A completed ``result`` whose ``meta`` JSON is exactly ``length`` bytes,
    its text ``field`` padded to the length."""

    def meta(text: str) -> str:
        return json.dumps({"status": "completed", "result": {**result, field: text}})

    text = meta("a" * (length - len(meta(""))))
    assert len(text) == length
    return text


class _Db:
    """The session the route runs on: the credential lookup left a
    transaction open, then the command row is locked."""

    def __init__(self, command: SimpleNamespace) -> None:
        self.command = command
        self.calls: list[str] = []
        self._open = True

    def in_transaction(self) -> bool:
        return self._open

    async def commit(self) -> None:
        self.calls.append("commit")
        self._open = False

    async def rollback(self) -> None:
        self.calls.append("rollback")
        self._open = False

    async def close(self) -> None:
        pass

    async def execute(self, _statement):
        self.calls.append("lock")
        return SimpleNamespace(scalar_one_or_none=lambda: self.command)


@pytest.fixture
def upload(monkeypatch):
    monkeypatch.setattr(runners_api.settings, "runners_enabled", True)
    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", 25_000_000)
    runner = SimpleNamespace(
        id=uuid.uuid4(), host_facts={}, name="laptop", platform="linux", agent_version="1"
    )
    command = SimpleNamespace(
        id=uuid.uuid4(),
        runner_id=runner.id,
        status="dispatched",
        kind="run_shell",
        conversation_id=None,
        assignment_run_id=None,
        user_id=None,
    )
    db = _Db(command)
    recorded: list[dict] = []

    async def _submit(command_id, *, status, result=None, error=None, result_file_id=None, db=None):
        recorded.append(
            {
                "command_id": command_id,
                "status": status,
                "result": result,
                "error": error,
                "result_file_id": result_file_id,
            }
        )
        return True

    monkeypatch.setattr(runner_transport, "submit_result", _submit)
    app = FastAPI()
    app.include_router(runners_api.router, prefix="/api/v1/runners")
    app.dependency_overrides[get_current_runner] = lambda: runner
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    def post(meta: str | None, files: list[tuple[str, bytes]] = ()):
        return client.post(
            f"/api/v1/runners/commands/{command.id}/result",
            content=_multipart(meta, files),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )

    return SimpleNamespace(
        post=post, app=app, db=db, command=command, runner=runner, recorded=recorded
    )


@pytest.fixture
def small_meta_limit(monkeypatch) -> int:
    """A ``meta`` limit of 4 KiB, so a refused upload needs no 16 MiB body."""
    monkeypatch.setattr(runner_transport, "RUNNER_MAX_RESULT_META_BYTES", 4096)
    return 4096


def test_the_meta_limit_is_the_runners():
    match = re.search(
        r"pub const MAX_RESULT_META_BYTES: usize = ([\d_ *]+);",
        (RUNNER_SRC / "protocol.rs").read_text(encoding="utf-8"),
    )
    assert match, "MAX_RESULT_META_BYTES not found in apps/runner/src/protocol.rs"
    runner_limit = math.prod(int(factor.replace("_", "")) for factor in match.group(1).split("*"))
    assert runner_limit == RUNNER_MAX_RESULT_META_BYTES
    assert RUNNER_MAX_RESULT_META_BYTES > FRAMEWORK_FIELD_LIMIT


def test_a_shell_result_over_the_framework_field_limit_is_accepted(upload):
    # Both streams at the runner's 1 MiB cap, with its truncation marker.
    stream = "a" * FRAMEWORK_FIELD_LIMIT + "\n...[output truncated at 1048576 bytes]"
    meta = json.dumps(
        {"status": "completed", "result": {"exit_code": 0, "stdout": stream, "stderr": stream}}
    )
    assert len(meta) > 2 * FRAMEWORK_FIELD_LIMIT

    response = upload.post(meta)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    [recorded] = upload.recorded
    assert recorded["status"] == "completed"
    assert recorded["result"]["stdout"] == stream
    # The credential lookup's transaction ended before the upload was read.
    assert upload.db.calls == ["commit", "lock"]


@pytest.mark.parametrize(
    ("kind", "result", "field"),
    [
        ("run_shell", SHELL_RESULT, "stdout"),
        # No older runner bounds a page's content: any of them may send it.
        ("browser_control", {"action": "get_content", "url": "https://example.com"}, "content"),
    ],
    ids=["shell-output", "page-content"],
)
def test_a_result_at_the_limit_is_accepted(upload, kind, result, field):
    upload.command.kind = kind

    response = upload.post(_meta_of_length(RUNNER_MAX_RESULT_META_BYTES, result, field))

    assert response.status_code == 200, response.text
    [recorded] = upload.recorded
    assert recorded["status"] == "completed"
    assert len(recorded["result"][field]) > RUNNER_MAX_RESULT_META_BYTES - 1024


def test_a_result_over_the_limit_fails_the_command_with_the_reason(upload):
    response = upload.post(_meta_of_length(RUNNER_MAX_RESULT_META_BYTES + 1))

    assert response.status_code == 400
    # The words the route tells a size refusal by, so a Starlette release that
    # rewords them fails here instead of giving the agent the wrong advice.
    detail = response.json()["detail"]
    assert runners_api._PART_TOO_LARGE in detail and "16384KB" in detail
    [recorded] = upload.recorded
    assert recorded["command_id"] == upload.command.id
    assert recorded["status"] == "failed"
    assert recorded["result"] is None
    assert recorded["error"].startswith("The platform refused this command's result: ")
    assert "may take at most 16 MiB once written as JSON" in recorded["error"]
    assert "ask the machine for less" in recorded["error"]
    assert upload.db.calls == ["commit", "lock"]


@pytest.mark.parametrize(
    ("command", "refused", "expected"),
    [
        ("finished", False, 200),
        ("finished", True, 400),
        ("another-runners", False, 404),
        ("another-runners", True, 404),
        ("awaiting-approval", False, 409),
        ("awaiting-approval", True, 409),
    ],
    ids=[
        "finished-valid",
        "finished-refused",
        "another-runners-valid",
        "another-runners-refused",
        "awaiting-approval-valid",
        "awaiting-approval-refused",
    ],
)
def test_a_result_for_a_command_not_awaiting_one_records_nothing(
    upload, small_meta_limit, command, refused, expected
):
    if command == "finished":
        upload.command.status = "completed"
    elif command == "another-runners":
        upload.command.runner_id = uuid.uuid4()
    else:
        upload.command.status = "pending_approval"
    meta = _meta_of_length(small_meta_limit + 1) if refused else '{"status": "completed"}'

    response = upload.post(meta)

    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json() == {"ok": True}
    # Nothing is written: a held command stays held for its approver.
    assert upload.recorded == []


def test_the_upload_is_read_only_once_the_runner_is_authenticated(upload):
    def _revoked():
        raise HTTPException(status_code=401, detail="Invalid or revoked runner credential")

    upload.app.dependency_overrides[get_current_runner] = _revoked

    response = upload.post(_meta_of_length(RUNNER_MAX_RESULT_META_BYTES + 1))

    assert response.status_code == 401
    assert upload.recorded == []
    assert upload.db.calls == []


@pytest.mark.parametrize(
    "meta", ["not json", "[1, 2]", '"completed"'], ids=["not-json", "a-list", "a-string"]
)
def test_an_unreadable_meta_fails_the_command(upload, meta):
    response = upload.post(meta)

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid meta JSON"
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"
    assert "not a JSON object" in recorded["error"]


def test_an_unsupported_status_fails_the_command(upload):
    response = upload.post(json.dumps({"status": "exploded"}))

    assert response.status_code == 422
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"
    assert "Unsupported result status 'exploded'" in recorded["error"]


def test_an_upload_without_its_meta_fails_the_command(upload):
    response = upload.post(None, [("a.bin", b"abc")])

    assert response.status_code == 422
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"


def test_a_second_file_fails_the_command(upload):
    response = upload.post('{"status": "completed"}', [("a.bin", b"a"), ("b.bin", b"b")])

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == "Too many files. Maximum number of files is 1."
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"
    # The parser's own words, and no advice to ask for less: size was not it.
    assert detail in recorded["error"]
    assert "at most one file" in recorded["error"]
    assert "ask the machine for less" not in recorded["error"]


def test_a_file_over_the_result_limit_fails_the_command(upload, monkeypatch):
    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", 10)

    response = upload.post('{"status": "completed"}', [("big.bin", b"x" * 11)])

    assert response.status_code == 413
    assert response.json()["detail"] == "Result file too large"
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"
    assert "RUNNER_MAX_RESULT_BYTES (10 bytes)" in recorded["error"]


def _persisted_files(monkeypatch) -> list[dict]:
    stored: list[dict] = []

    async def _persist(**kwargs):
        stored.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    return stored


def test_a_file_larger_than_the_meta_limit_is_bounded_by_its_own(upload, monkeypatch):
    # ``max_part_size`` bounds the text field only: a file between the 16 MiB
    # of ``meta`` and RUNNER_MAX_RESULT_BYTES is stored.
    stored = _persisted_files(monkeypatch)
    upload.command.kind = "read_file"
    content = b"x" * (RUNNER_MAX_RESULT_META_BYTES + 1)

    response = upload.post('{"status": "completed", "result": {"size": 1}}', [("big.bin", content)])

    assert response.status_code == 200, response.text
    assert [len(s["raw_bytes"]) for s in stored] == [len(content)]
    [recorded] = upload.recorded
    assert recorded["status"] == "completed"
    assert recorded["result"]["byte_size"] == len(content)


def test_a_file_past_the_meta_limit_and_its_own_is_refused_as_a_file(upload, monkeypatch):
    stored = _persisted_files(monkeypatch)
    limit = RUNNER_MAX_RESULT_META_BYTES + 16
    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", limit)

    response = upload.post('{"status": "completed"}', [("big.bin", b"x" * (limit + 1))])

    assert response.status_code == 413
    assert response.json()["detail"] == "Result file too large"
    assert stored == []
    [recorded] = upload.recorded
    assert f"RUNNER_MAX_RESULT_BYTES ({limit:,} bytes)" in recorded["error"]
    assert "once written as JSON" not in recorded["error"]


def test_the_largest_valid_upload_fits_the_body_limit(upload, small_meta_limit, monkeypatch):
    stored = _persisted_files(monkeypatch)
    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", 4096)
    upload.command.kind = "read_file"
    # Both parts at their limits, under a long file name.
    files = [("n" * 255 + ".bin", b"x" * 4096)]

    response = upload.post(_meta_of_length(small_meta_limit), files)

    assert response.status_code == 200, response.text
    assert [len(s["raw_bytes"]) for s in stored] == [4096]


async def test_an_upload_past_the_body_limit_is_refused_before_it_is_read(
    upload, small_meta_limit, monkeypatch
):
    monkeypatch.setattr(runners_api.settings, "runner_max_result_bytes", 4096)
    monkeypatch.setattr(runners_api, "_RESULT_UPLOAD_FRAMING_BYTES", 1024)
    limit = small_meta_limit + 4096 + 1024
    chunk = 1024
    body = _multipart('{"status": "completed"}', [("huge.bin", b"x" * (256 * chunk))])
    sent_chunks = 0

    async def stream():
        nonlocal sent_chunks
        for start in range(0, len(body), chunk):
            sent_chunks += 1
            yield body[start : start + chunk]

    transport = httpx.ASGITransport(app=upload.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform") as client:
        response = await client.post(
            f"/api/v1/runners/commands/{upload.command.id}/result",
            content=stream(),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Result upload too large"
    # Read up to the limit, not to the end of the 256 KiB it was sent.
    assert sent_chunks <= limit // chunk + 2 < len(body) // chunk
    [recorded] = upload.recorded
    assert recorded["status"] == "failed"
    assert f"larger than {limit:,} bytes" in recorded["error"]
    assert "RUNNER_MAX_RESULT_BYTES (4,096 bytes)" in recorded["error"]


async def test_a_dropped_upload_is_left_for_the_runner_to_retry(upload):
    # The runner retries an upload whose connection broke (``is_transient`` in
    # ``apps/runner/src/api.rs``): failing the command here would throw away
    # the result that retry brings.
    body = _multipart('{"status": "completed"}', [("a.bin", b"x" * 4096)])
    messages = [{"type": "http.request", "body": body[:512], "more_body": True}]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    path = f"/api/v1/runners/commands/{upload.command.id}/result"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("platform", 80),
    }

    await upload.app(scope, receive, send)

    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 400
    assert upload.recorded == []
    # The command row was never even locked.
    assert upload.db.calls == ["commit"]


def test_a_result_with_its_file_is_recorded(upload, monkeypatch):
    stored: list[dict] = []
    file_id = uuid.uuid4()

    async def _persist(**kwargs):
        stored.append(kwargs)
        return SimpleNamespace(id=file_id)

    monkeypatch.setattr(_shared, "_persist_generated_file", _persist)
    upload.command.kind = "read_file"
    meta = json.dumps({"status": "completed", "result": {"path": "/home/me/a.bin", "size": 3}})

    response = upload.post(meta, [("a.bin", b"abc")])

    assert response.status_code == 200, response.text
    assert [(s["filename"], s["raw_bytes"]) for s in stored] == [("a.bin", b"abc")]
    [recorded] = upload.recorded
    assert recorded["status"] == "completed"
    assert recorded["result_file_id"] == file_id
    assert recorded["result"]["byte_size"] == 3
