"""Regression tests for MCP tool loading helpers."""

import asyncio
import functools
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services import sub_agent
from app.services.builtin_tools import (
    _invalid_task_id_message,
    _platform_label,
    build_image_generation_tool,
    build_sub_agent_tools,
    load_mcp_tools,
)
from app.tools.base import ToolCall


def _async_runtime(build):
    """``build_mcp_runtime`` is a coroutine (it reads vault references)."""

    async def _build(server, _credential=None):
        return build(server)

    return _build


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, _query: Any) -> _FakeExecuteResult:
        return _FakeExecuteResult(self._rows)

    def in_transaction(self) -> bool:
        # ``load_mcp_tools`` calls ``release_db_connection_before_loop(db)``
        # before connecting uncached servers; report no open transaction so
        # the helper is a clean no-op (no reliance on its catch-all).
        return False


class _ImageToolDB:
    """Caller session passed to ``build_image_generation_tool``.

    The generate_image closure must NEVER write on the caller
    session (it runs mid agentic loop, after the caller released its pooled
    connection) — any ``add``/``flush`` on it is a regression.
    """

    def add(self, obj: Any) -> None:
        raise AssertionError(
            "generate_image must not write on the caller session"
        )

    async def flush(self) -> None:
        raise AssertionError(
            "generate_image must not flush the caller session"
        )


class _FakeOwnSession:
    """Stand-in for the dedicated short-lived persistence session."""

    def __init__(self, sink: list) -> None:
        self._sink = sink
        self.committed = False

    async def __aenter__(self) -> "_FakeOwnSession":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def add(self, obj: Any) -> None:
        self._sink.append(obj)

    async def commit(self) -> None:
        self.committed = True


def test_platform_label_uses_cached_branding(monkeypatch):
    """Tool descriptions should follow the central branding resolver."""
    from app.services import branding as branding_mod
    from app.services.branding import Branding

    monkeypatch.setattr(branding_mod, "_cached_branding", Branding(name="Acme AI"))

    assert _platform_label() == "Acme AI"


@pytest.mark.asyncio
async def test_generate_image_uses_preassigned_ids_without_flushing(monkeypatch):
    db = _ImageToolDB()
    provider = SimpleNamespace(generate_image=AsyncMock(return_value=b"png-bytes"))

    async def _fake_get_image_provider(_db: Any) -> tuple[Any, str]:
        return provider, "gpt-image-1"

    upload_mock = AsyncMock(return_value="generated/test-key")
    monkeypatch.setattr("app.llm.router.get_image_provider", _fake_get_image_provider)
    monkeypatch.setattr("app.services.file_storage.file_storage.upload", upload_mock)
    added: list[Any] = []
    monkeypatch.setattr(
        "app.services.builtin_tools.media._own_session",
        lambda: _FakeOwnSession(added),
    )

    _, executor = await build_image_generation_tool(
        db, str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="generate_image",
            arguments={"prompt": "test prompt", "size": "1024x1024"},
        )
    )

    assert not result.is_error
    assert "/api/v1/chat/files/" in result.content
    assert result.files is not None
    assert len(result.files) == 1

    file_attachment = next(obj for obj in added if obj.__class__.__name__ == "FileAttachment")
    chat_image = next(obj for obj in added if obj.__class__.__name__ == "ChatImage")
    assert file_attachment.id is not None
    assert chat_image.id is not None


@pytest.mark.asyncio
async def test_generate_image_s3_fallback_uses_chat_image_url_without_flushing(monkeypatch):
    db = _ImageToolDB()
    provider = SimpleNamespace(generate_image=AsyncMock(return_value=b"png-bytes"))

    async def _fake_get_image_provider(_db: Any) -> tuple[Any, str]:
        return provider, "gpt-image-1"

    upload_mock = AsyncMock(side_effect=RuntimeError("s3 unavailable"))
    monkeypatch.setattr("app.llm.router.get_image_provider", _fake_get_image_provider)
    monkeypatch.setattr("app.services.file_storage.file_storage.upload", upload_mock)
    added: list[Any] = []
    monkeypatch.setattr(
        "app.services.builtin_tools.media._own_session",
        lambda: _FakeOwnSession(added),
    )

    _, executor = await build_image_generation_tool(
        db, str(uuid.uuid4()), str(uuid.uuid4())
    )
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="generate_image",
            arguments={"prompt": "fallback prompt"},
        )
    )

    assert not result.is_error
    assert "/api/v1/chat/images/" in result.content
    assert result.files is None

    chat_image = next(obj for obj in added if obj.__class__.__name__ == "ChatImage")
    assert chat_image.id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_bytes", "content_type", "extension"),
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "image/jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", "image/png", ".png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", ".webp"),
        (b"not-an-image", "image/png", ".png"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/png", ".png"),
    ],
)
async def test_generate_image_stores_the_type_of_its_bytes(monkeypatch, image_bytes, content_type, extension):
    """Gemini answers JPEG: the S3 object, the attachment and the ChatImage carry the bytes' type (#747)."""
    provider = SimpleNamespace(generate_image=AsyncMock(return_value=image_bytes))

    async def _fake_get_image_provider(_db: Any) -> tuple[Any, str]:
        return provider, "gemini-3-pro-image-preview"

    upload_mock = AsyncMock(return_value="generated/test-key")
    monkeypatch.setattr("app.llm.router.get_image_provider", _fake_get_image_provider)
    monkeypatch.setattr("app.services.file_storage.file_storage.upload", upload_mock)
    added: list[Any] = []
    monkeypatch.setattr("app.services.builtin_tools.media._own_session", lambda: _FakeOwnSession(added))

    _, executor = await build_image_generation_tool(_ImageToolDB(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="generate_image", arguments={"prompt": "a desk"})
    )

    assert not result.is_error
    upload = upload_mock.await_args.kwargs
    assert upload["content_type"] == content_type and upload["filename"].endswith(extension)
    file_attachment = next(obj for obj in added if obj.__class__.__name__ == "FileAttachment")
    assert file_attachment.content_type == content_type
    assert file_attachment.filename == upload["filename"]
    chat_image = next(obj for obj in added if obj.__class__.__name__ == "ChatImage")
    assert chat_image.content_type == content_type


class _ProviderError(Exception):
    """An OpenAI-shaped SDK error: its HTTP status, its JSON body, its response headers."""

    def __init__(self, status: int, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status
        self.body = {"error": {"message": message}}
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})


async def _image_call(monkeypatch, generate) -> Any:
    async def _fake_get_image_provider(_db: Any) -> tuple[Any, str]:
        return SimpleNamespace(generate_image=generate), "gemini-3-pro-image"

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.llm.router.get_image_provider", _fake_get_image_provider)
    monkeypatch.setattr("app.services.file_storage.file_storage.upload", AsyncMock(return_value="generated/key"))
    monkeypatch.setattr("app.services.builtin_tools.media._own_session", lambda: _FakeOwnSession([]))
    monkeypatch.setattr("app.llm.images.asyncio.sleep", no_wait)
    _, executor = await build_image_generation_tool(_ImageToolDB(), str(uuid.uuid4()), str(uuid.uuid4()))
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="generate_image", arguments={"prompt": "a desk, its screen solid green"})
    )


@pytest.mark.asyncio
async def test_generate_image_asks_again_through_a_rate_limit_and_names_its_file(monkeypatch):
    """A 429 is asked again in the call; the image made is named by its file id."""
    generate = AsyncMock(side_effect=[_ProviderError(429, "Quota exceeded", retry_after="2"), b"png-bytes"])
    result = await _image_call(monkeypatch, generate)
    assert not result.is_error and generate.await_count == 2
    assert result.content.startswith("Image generated successfully (file_id ")


@pytest.mark.asyncio
async def test_generate_image_says_the_providers_status_and_words_and_is_an_outcome(monkeypatch):
    """No "try again later": the model, the HTTP status, the provider's message; counted per call."""
    generate = AsyncMock(side_effect=_ProviderError(400, "Image generation blocked by the safety filter"))
    result = await _image_call(monkeypatch, generate)
    assert result.is_error and result.outcome_failure and generate.await_count == 1
    assert result.content == (
        "Image generation failed with gemini-3-pro-image (HTTP 400): Image generation blocked by the safety filter"
    )
    # A provider still refusing after the retries, or asking to wait longer than a call waits, is said the same way.
    busy = AsyncMock(side_effect=_ProviderError(503, "overloaded"))
    assert "(HTTP 503): overloaded" in (await _image_call(monkeypatch, busy)).content and busy.await_count == 3
    patient = AsyncMock(side_effect=_ProviderError(429, "Quota exceeded", retry_after="600"))
    assert "(HTTP 429)" in (await _image_call(monkeypatch, patient)).content and patient.await_count == 1


def test_load_mcp_tools_skips_cancelled_connect_result(monkeypatch):
    mcp_id = uuid.uuid4()
    fake_server = SimpleNamespace(
        id=mcp_id, name="Screaming Frog", discovered_tools=None
    )
    db = _FakeDB([fake_server])
    add_calls: list[list[Any]] = []
    recorded_failures: list[str] = []

    class FakeMCPManager:
        def __init__(self, artifact_scope: Any = None) -> None:
            self.artifact_scope = artifact_scope

        async def connect_server(self, _runtime: Any) -> list[Any]:
            raise asyncio.CancelledError()

        def register_cached_server(self, _runtime: Any, _cached_tools: Any) -> list[Any]:
            raise AssertionError("register_cached_server should not be called")

        def record_failed_server(self, name: str, *, hours: int | None = None) -> None:
            recorded_failures.append(name)

    class FakeComposite:
        def add_tools(self, tools: list[Any], _executor: Any) -> None:
            add_calls.append(tools)

    monkeypatch.setattr("app.tools.mcp_client.MCPManager", FakeMCPManager)
    monkeypatch.setattr("app.api.v1.helpers.build_mcp_runtime", _async_runtime(lambda srv: srv))

    returned_manager = asyncio.run(load_mcp_tools(db, [mcp_id], FakeComposite()))

    assert returned_manager is not None
    assert add_calls == []
    # A CancelledError does NOT mean the server is unreachable (the connect
    # task can be cancelled by an overall run cancellation/timeout) — it
    # must NOT be recorded as a failed server.
    assert recorded_failures == []


def test_load_mcp_tools_skips_an_entry_that_is_not_a_server_id(monkeypatch):
    """An entry that names no server (a bundle-local reference stored by an
    older import, a redacted placeholder) is skipped on its own: it must not
    take every other server of the list down with it."""
    from app.tools.mcp_client import MCPServerRuntime

    mcp_id = uuid.uuid4()
    fake_server = SimpleNamespace(
        id=mcp_id,
        name="Docs",
        discovered_tools=[{"name": "fetch_url", "description": "Fetch a URL"}],
    )
    add_calls: list[list[Any]] = []

    class FakeComposite:
        def add_tools(self, tools: list[Any], _executor: Any) -> None:
            add_calls.append(tools)

    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(
            lambda srv: MCPServerRuntime(
                name=srv.name, transport="streamable_http", url="http://localhost:9999/mcp"
            )
        ),
    )

    manager = asyncio.run(
        load_mcp_tools(
            _FakeDB([fake_server]),
            ["mcp-0", str(mcp_id), "redacted-mcp-server"],
            FakeComposite(),
        )
    )

    assert manager is not None
    assert [[t.name for t in tools] for tools in add_calls] == [["fetch_url"], ["read_mcp_resource"]]
    assert asyncio.run(load_mcp_tools(_FakeDB([]), ["mcp-0"], FakeComposite())) is None


def test_invalid_task_id_message_truncates_very_long_input():
    raw = "x" * 500
    message = _invalid_task_id_message(raw)

    assert "Invalid task_id format: expected a UUID string" in message
    assert len(message) < 260
    assert "…" in message


# Use an oversized-but-cheap payload to exercise the truncation path:
# `_TASK_ID_DISPLAY_BUDGET` is ~120 chars, so 8 KB is comfortably above the
# budget (~70x) without paying CI memory cost for multi-MB allocations.
# Assertions focus on the *bounded output* contract (prefix + ellipsis +
# tight upper bound on the final message) rather than on the input size.
_OVERSIZED_TASK_ID_TEST_LEN = 8 * 1024


def test_invalid_task_id_message_bounds_large_string_before_repr():
    """A hallucinated oversized ``task_id`` must NOT cause
    ``_invalid_task_id_message`` to round-trip the full input through
    ``repr()`` just to truncate the result back down to ~120 chars.

    The bounded contract: regardless of input size the output is well
    under 1 KB, and (more importantly) the function returns in
    O(budget) time/memory rather than O(input size).
    """
    raw = "x" * _OVERSIZED_TASK_ID_TEST_LEN
    message = _invalid_task_id_message(raw)

    assert "Invalid task_id format: expected a UUID string" in message
    # Bounded output: even at the max budget + repr quoting + prefix,
    # the final message stays comfortably under a kilobyte.
    assert len(message) < 1024, (
        f"oversized task_id must produce a bounded message; observed "
        f"{len(message)} chars"
    )
    assert "…" in message


def test_invalid_task_id_message_bounds_large_bytes_before_repr():
    """Same DoS-protection contract as for ``str`` but applied to
    ``bytes`` / ``bytearray`` — a hallucinated oversized binary blob
    must not balloon the formatter's allocation just to display the
    type/prefix back to the caller.
    """
    raw = b"x" * _OVERSIZED_TASK_ID_TEST_LEN
    message = _invalid_task_id_message(raw)

    assert "Invalid task_id format: expected a UUID string" in message
    assert len(message) < 1024


def test_invalid_task_id_message_avoids_expensive_repr_on_containers():
    """For ``dict`` / ``list`` / ``set`` / arbitrary objects the
    formatter must NOT walk the structure (``repr`` on a 10 K-entry
    dict allocates the full key/value string).  Use a bounded
    ``<X>`` type-name placeholder (e.g. ``<dict>``) instead so the
    message stays O(1).
    """
    huge_dict = {i: i for i in range(10_000)}
    huge_list = list(range(10_000))

    dict_message = _invalid_task_id_message(huge_dict)
    list_message = _invalid_task_id_message(huge_list)

    for message, type_name in (
        (dict_message, "dict"),
        (list_message, "list"),
    ):
        assert "Invalid task_id format: expected a UUID string" in message
        # Type-placeholder fast path: a bounded ``<X>`` string (e.g.
        # ``<dict>``), NOT a walked container repr.
        assert f"<{type_name}>" in message, (
            f"container input must produce a bounded ``<{type_name}>`` "
            f"placeholder; got {message!r}"
        )
        # No element from the huge structure should have leaked in.
        assert "9999" not in message
        assert len(message) < 200


def test_invalid_task_id_message_preserves_repr_for_atomic_scalars():
    """For atomic scalars (``int``, ``float``, ``bool``, ``None``)
    ``repr`` is already O(1)-ish and the LLM seeing the actual value
    is far more useful than a generic ``<int>`` placeholder, so we
    keep the literal repr.
    """
    for raw, expected in (
        (12345, "12345"),
        (3.14, "3.14"),
        (True, "True"),
        (None, "None"),
    ):
        message = _invalid_task_id_message(raw)
        assert expected in message, (
            f"atomic scalar {raw!r} should round-trip its repr in the "
            f"error message; got {message!r}"
        )


@pytest.mark.asyncio
async def test_sub_agent_tools_return_friendly_message_for_invalid_task_id(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _unexpected_statuses(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "get_task_statuses should not be called for invalid UUID task ids"
        )

    async def _unexpected_result(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("get_task_result should not be called for invalid UUID task ids")

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _unexpected_statuses)
    monkeypatch.setattr("app.services.sub_agent.get_task_result", _unexpected_result)

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    invalid_task_id = "not-a-uuid"
    expected = _invalid_task_id_message(invalid_task_id)

    status_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="check_task_status",
            arguments={"task_ids": invalid_task_id},
        )
    )
    result_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_task_result",
            arguments={"task_ids": invalid_task_id},
        )
    )
    full_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_full_task_result",
            arguments={"task_id": invalid_task_id},
        )
    )

    assert not status_res.is_error
    assert not result_res.is_error
    assert not full_res.is_error
    assert status_res.content == expected
    assert result_res.content == expected
    assert full_res.content == expected


@pytest.mark.asyncio
async def test_sub_agent_tools_return_not_found_for_valid_but_unknown_task_id(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _missing_statuses(task_ids: Any, *_args: Any, **_kwargs: Any) -> Any:
        # Batched fetch used by ``check_task_status`` -> ``wait_for_tasks``:
        # every requested id maps to None (unknown task).
        return {str(t).strip(): None for t in task_ids}

    async def _missing_result(*_args: Any, **_kwargs: Any) -> Any:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _missing_statuses)
    monkeypatch.setattr("app.services.sub_agent.get_task_result", _missing_result)
    # An unknown id stays pending, so the wait runs out its whole budget - on
    # the real monotonic clock, which the no-op sleep above cannot advance.
    monkeypatch.setattr(
        "app.services.sub_agent.wait_for_tasks",
        functools.partial(sub_agent.wait_for_tasks, budget=0.0),
    )

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    task_id = str(uuid.uuid4())
    status_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="check_task_status",
            arguments={"task_ids": task_id},
        )
    )
    result_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_task_result",
            arguments={"task_ids": task_id},
        )
    )
    full_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_full_task_result",
            arguments={"task_id": task_id},
        )
    )

    assert status_res.content == f"{task_id[:12]}: not found"
    assert result_res.content == f"{task_id[:12]}: not found"
    assert full_res.content == f"Task {task_id} not found."


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_task_id", ["", None, 12345, "550e8400-e29b-41d4-a716", "550e8400-e29b-41d4-a716-zzzzzzzzzzzz"])
async def test_get_full_task_result_rejects_edge_case_invalid_ids(monkeypatch, invalid_task_id):
    async def _unexpected_result(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("get_task_result should not be called for invalid UUID task ids")

    monkeypatch.setattr("app.services.sub_agent.get_task_result", _unexpected_result)

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    full_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_full_task_result",
            arguments={"task_id": invalid_task_id},
        )
    )

    assert full_res.content == _invalid_task_id_message(invalid_task_id)


def _sections_answer() -> str:
    """A 120,000-character answer of three sections with distinct markers."""
    return "<<A>>" + "a" * 39_989 + "<</A>>" + "<<B>>" + "b" * 39_989 + "<</B>>" + "<<C>>" + "c" * 39_989 + "<</C>>"


def _completed_executor(monkeypatch, answer: str):
    async def _result(*_args: Any, **_kwargs: Any) -> Any:
        return {"task_id": "t", "status": "completed", "result": answer, "error": None, "result_file_ids": []}

    monkeypatch.setattr("app.services.sub_agent.get_task_result", _result)
    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None
    return executor


async def _full(executor, **arguments: Any) -> str:
    res = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name="get_full_task_result", arguments=arguments))
    assert not res.is_error
    return res.content


@pytest.mark.asyncio
async def test_get_full_task_result_pages_a_long_answer_to_its_end(monkeypatch):
    """#1394: a 120,000-character answer is read whole, A, B and C, by offset;
    every read names the total and the next offset, the last says the end."""
    answer = _sections_answer()
    assert len(answer) == 120_000
    executor = _completed_executor(monkeypatch, answer)
    task_id = str(uuid.uuid4())

    first = await _full(executor, task_id=task_id)
    assert first.startswith("[Task answer: characters 0 to 50,000 of 120,000; read the rest with ")
    assert f'get_full_task_result(task_id="{task_id}", offset=50000)' in first

    second = await _full(executor, task_id=task_id, offset=50_000)
    assert "characters 50,000 to 100,000 of 120,000" in second
    assert "offset=100000" in second

    third = await _full(executor, task_id=task_id, offset=100_000)
    assert "characters 100,000 to 120,000 of 120,000; this is the end." in third

    read = "".join(part.split("\n\n", 1)[1] for part in (first, second, third))
    assert read == answer
    for marker in ("<<A>>", "<</A>>", "<<B>>", "<</B>>", "<<C>>", "<</C>>"):
        assert marker in read


@pytest.mark.asyncio
async def test_get_full_task_result_honours_max_chars(monkeypatch):
    executor = _completed_executor(monkeypatch, _sections_answer())
    out = await _full(executor, task_id=str(uuid.uuid4()), offset=0, max_chars=1_000)
    assert "characters 0 to 1,000 of 120,000" in out
    assert "offset=1000" in out


@pytest.mark.asyncio
async def test_get_full_task_result_short_answer_is_unchanged(monkeypatch):
    """An answer under one window reads exactly as before: no header."""
    executor = _completed_executor(monkeypatch, "short answer")
    assert await _full(executor, task_id=str(uuid.uuid4())) == "short answer"


@pytest.mark.asyncio
async def test_get_task_result_says_a_long_answer_pages(monkeypatch):
    executor = _completed_executor(monkeypatch, _sections_answer())
    task_id = str(uuid.uuid4())
    res = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="get_task_result", arguments={"task_ids": task_id})
    )
    assert "completed (120,000 chars)" in res.content
    assert f'get_full_task_result(task_id="{task_id}", offset=N)' in res.content


@pytest.mark.asyncio
async def test_get_task_result_short_answer_has_no_paging_note(monkeypatch):
    executor = _completed_executor(monkeypatch, "short answer")
    res = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="get_task_result", arguments={"task_ids": str(uuid.uuid4())})
    )
    assert "offset=N" not in res.content


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_task_ids", [None, 12345])
async def test_multi_task_tools_reject_non_string_task_ids(monkeypatch, bad_task_ids):
    async def _unexpected_statuses(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "get_task_statuses should not be called for invalid UUID task ids"
        )

    async def _unexpected_result(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("get_task_result should not be called for invalid UUID task ids")

    monkeypatch.setattr("app.services.sub_agent.get_task_statuses", _unexpected_statuses)
    monkeypatch.setattr("app.services.sub_agent.get_task_result", _unexpected_result)

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    status_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="check_task_status",
            arguments={"task_ids": bad_task_ids},
        )
    )
    result_res = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_task_result",
            arguments={"task_ids": bad_task_ids},
        )
    )

    expected = _invalid_task_id_message(bad_task_ids)
    assert status_res.content == expected
    assert result_res.content == expected


@pytest.mark.asyncio
async def test_check_task_status_skips_wait_when_all_ids_invalid(monkeypatch):
    """When every task_id is malformed there is nothing real to wait on, so
    ``check_task_status`` returns instantly without entering the adaptive wait
    (no ``get_task_statuses`` query, no sleep). The blind 15s poll-spacing sleep
    was removed in favour of ``wait_for_tasks``; this locks the all-invalid
    short-circuit that precedes it.
    """
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def _unexpected_statuses(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "get_task_statuses should not be called when every task_id is invalid"
        )

    monkeypatch.setattr("asyncio.sleep", _record_sleep)
    monkeypatch.setattr(
        "app.services.sub_agent.get_task_statuses", _unexpected_statuses
    )

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="check_task_status",
            arguments={"task_ids": "not-a-uuid,also-bad"},
        )
    )

    assert not result.is_error
    # Nothing valid to wait on -> no poll spacing sleep at all (least of all 15s).
    assert sleep_calls == []
    # Both invalid ids should produce their own validation message.
    assert result.content.count("Invalid task_id format") == 2


@pytest.mark.asyncio
async def test_check_task_status_waits_only_on_valid_ids_in_mixed_batch(monkeypatch):
    """A batch mixing a UUID-shaped id with a malformed one adaptively waits on
    ONLY the valid id (the fixed 15s blind sleep was replaced by
    ``wait_for_tasks``) and still emits a per-id line for the invalid one.
    Locking this prevents a regression back to the fixed delay, and ensures the
    junk id is never handed to the wait.
    """
    sleep_calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    waited_with: list[list[str]] = []

    async def _fake_wait(
        task_ids: Any, _user_id: Any, *_args: Any, **_kwargs: Any
    ) -> Any:
        ids = list(task_ids)
        waited_with.append(ids)
        return {tid: {"status": "completed"} for tid in ids}

    monkeypatch.setattr("asyncio.sleep", _record_sleep)
    monkeypatch.setattr("app.services.sub_agent.wait_for_tasks", _fake_wait)

    _, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None

    valid_task_id = str(uuid.uuid4())
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="check_task_status",
            arguments={"task_ids": f"{valid_task_id},not-a-uuid"},
        )
    )

    assert not result.is_error
    # Adaptive wait fired exactly once, scoped to the valid id only.
    assert waited_with == [[valid_task_id]]
    # The valid id reports its resolved status; the invalid id gets its message.
    assert f"{valid_task_id[:12]}: completed" in result.content
    assert "Invalid task_id format" in result.content
    # The removed fixed 15s blind sleep must never come back.
    assert 15 not in sleep_calls


@pytest.mark.asyncio
async def test_clear_collected_removes_var_and_persists():
    """`clear_collected` removes `$collected` from the in-memory store and calls
    `session_persist` so the cleared state mirrors to Redis (unblocking
    `transfer_to_agent`). A second call is a no-op (nothing to clear)."""
    session_vars: dict = {"$collected": {"rules": {"_item_0": {"name": "n0"}}}}
    persist_calls: list[int] = []

    async def _persist() -> None:
        persist_calls.append(1)

    definitions, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
        session_vars=session_vars,
        session_persist=_persist,
    )
    assert executor is not None
    # The tool is exposed only when a session store is provided.
    assert any(d.name == "clear_collected" for d in definitions)

    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="clear_collected", arguments={})
    )
    assert not result.is_error
    assert "$collected" not in session_vars
    assert persist_calls == [1]

    # Idempotent: clearing again reports nothing to clear and does not re-persist.
    result2 = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="clear_collected", arguments={})
    )
    assert not result2.is_error
    assert "Nothing to clear" in result2.content
    assert persist_calls == [1]


@pytest.mark.asyncio
async def test_clear_collected_not_exposed_without_session_vars():
    """Without a session store the tool is not advertised (mirrors
    `collect_task_results`)."""
    definitions, executor = build_sub_agent_tools(
        db=SimpleNamespace(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[SimpleNamespace(id=uuid.uuid4(), name="Helper")],
    )
    assert executor is not None
    assert not any(d.name == "clear_collected" for d in definitions)
