from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.services import langfuse_tracing as lft


class _Span:
    def __init__(self) -> None:
        self.trace_id = "trace-id"
        self.id = "span-id"

    def update(self, **_kwargs) -> None:
        return None

    def end(self) -> None:
        return None


def test_end_trace_does_not_propagate_local_dispatch_errors(monkeypatch):
    def _raise_submit(*_args, **_kwargs):
        raise RuntimeError("dispatcher down")

    monkeypatch.setattr(
        "app.services.tracing.dispatcher.submit_trace_for_flush",
        _raise_submit,
    )

    now = datetime.now(UTC)
    ctx = {
        "trace": _Span(),
        "generation": None,
        "metadata": {},
        "parent_ctx": None,
        "local_trace_id": uuid4(),
        "local_source": "assignment",
        "local_name": "assignment:test",
        "local_started_at": now,
        "local_status": "ok",
        "local_model": "claude-sonnet-4.6",
        "local_input": {"messages": []},
        "local_metadata": {"provider": "anthropic"},
        "local_tags": ["source:assignment"],
        "local_spans": [],
        "local_user_id": uuid4(),
        "local_agent_id": uuid4(),
        "local_assignment_run_id": uuid4(),
    }

    lft.end_trace(
        ctx,
        output={"text": "ok"},
        usage={"input_tokens": 12, "output_tokens": 5},
        model="claude-sonnet-4.6",
    )


def test_close_observation_preserves_non_error_level_in_local_span():
    class _Obs:
        def update(self, **_kwargs):
            return None

        def end(self):
            return None

    now = datetime.now(UTC)
    ctx = {
        "trace": _Obs(),
        "local_trace_id": uuid4(),
        "local_spans": [],
        "local_span_id": uuid4(),
        "local_parent_span_id": None,
        "local_span_type": "SPAN",
        "local_name": "test-span",
        "local_started_at": now,
        "local_input": {"k": "v"},
        "local_metadata": {},
        "local_model": "claude-sonnet-4.6",
        "local_position": 1,
    }

    lft._close_observation(ctx, output={"ok": True}, level="WARNING")

    assert len(ctx["local_spans"]) == 1
    assert ctx["local_spans"][0]["level"] == "WARNING"
