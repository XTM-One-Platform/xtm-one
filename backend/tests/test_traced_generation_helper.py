"""Unit tests for the ``traced_generation`` async context manager."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import langfuse_tracing as lft


def _make_parent_ctx_with_span() -> tuple[dict, MagicMock, MagicMock]:
    """Build a fake parent ctx + child observation mock."""
    child_span = MagicMock()
    parent_span = MagicMock()
    parent_span.start_observation = MagicMock(return_value=child_span)
    parent_ctx = {
        "trace": parent_span,
        "generation": None,
        "parent_ctx": None,
        "client": None,
    }
    return parent_ctx, parent_span, child_span


@pytest.mark.asyncio
async def test_traced_generation_success_closes_with_output_and_usage():
    parent_ctx, parent_span, child_span = _make_parent_ctx_with_span()

    async with lft.traced_generation(
        parent_ctx,
        name="my_call",
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    ) as gen:
        gen.response = SimpleNamespace(
            content="hello back",
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            model="gpt-4o-actual",
        )

    parent_span.start_observation.assert_called_once()
    kwargs = parent_span.start_observation.call_args.kwargs
    assert kwargs["name"] == "my_call"
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt-4o"

    # On exit, span.update was called with output/usage/model, then end().
    update_calls = child_span.update.call_args_list
    assert any(
        c.kwargs.get("output") == "hello back" for c in update_calls
    ), f"expected output in update calls, got: {update_calls}"
    assert any("usage_details" in c.kwargs for c in update_calls)
    # Model resolves to response.model (overrides constructor arg).
    assert any(
        c.kwargs.get("model") == "gpt-4o-actual" for c in update_calls
    )
    child_span.end.assert_called_once()


@pytest.mark.asyncio
async def test_traced_generation_exception_marks_error_and_reraises():
    parent_ctx, _parent_span, child_span = _make_parent_ctx_with_span()

    with pytest.raises(RuntimeError, match="upstream broke"):
        async with lft.traced_generation(
            parent_ctx, name="failing_call", model="gpt-4o"
        ):
            raise RuntimeError("upstream broke")

    # error path: status_message + level=ERROR set on the child span.
    update_calls = child_span.update.call_args_list
    assert any(
        c.kwargs.get("level") == "ERROR" for c in update_calls
    ), f"expected ERROR level on failure, got: {update_calls}"
    assert any(
        "RuntimeError" in (c.kwargs.get("status_message") or "")
        for c in update_calls
    )
    child_span.end.assert_called_once()


@pytest.mark.asyncio
async def test_traced_generation_no_parent_is_noop(monkeypatch):
    """No parent ctx + no contextvar → ``start_direct_generation`` returns
    ``None`` and the finally block must NOT call ``end_span``."""
    assert lft.get_current_trace_ctx() is None
    end_mock = MagicMock()
    monkeypatch.setattr(lft, "end_span", end_mock)

    async with lft.traced_generation(
        name="standalone", model="gpt-4o"
    ) as gen:
        gen.response = SimpleNamespace(content="ok", usage=None, model=None)

    end_mock.assert_not_called()


@pytest.mark.asyncio
async def test_traced_generation_falls_back_to_contextvar():
    parent_ctx, parent_span, _child_span = _make_parent_ctx_with_span()
    token = lft.set_current_trace_ctx(parent_ctx)
    try:
        async with lft.traced_generation(name="ctxvar_call", model="m"):
            pass
    finally:
        lft.reset_current_trace_ctx(token)
    parent_span.start_observation.assert_called_once()
    assert (
        parent_span.start_observation.call_args.kwargs["name"] == "ctxvar_call"
    )


@pytest.mark.asyncio
async def test_traced_generation_custom_name_propagates():
    parent_ctx, parent_span, _child_span = _make_parent_ctx_with_span()
    async with lft.traced_generation(
        parent_ctx, name="activation_gate", model="gpt-4o"
    ):
        pass
    assert (
        parent_span.start_observation.call_args.kwargs["name"]
        == "activation_gate"
    )


@pytest.mark.asyncio
async def test_traced_generation_explicit_handle_fields_override_response():
    """Caller can set ``handle.content`` / ``handle.usage`` directly for
    non-``ChatResponse`` shapes (e.g. raw provider dicts)."""
    parent_ctx, _parent_span, child_span = _make_parent_ctx_with_span()
    async with lft.traced_generation(
        parent_ctx, name="raw_call", model="m"
    ) as gen:
        gen.content = "explicit output"
        gen.usage = {"prompt_tokens": 1, "completion_tokens": 1}
        gen.model = "explicit-model"

    update_calls = child_span.update.call_args_list
    assert any(
        c.kwargs.get("output") == "explicit output" for c in update_calls
    )
    assert any(
        c.kwargs.get("model") == "explicit-model" for c in update_calls
    )
