"""Contract tests: the autonomous control plane parses provider RESULT PROSE.

Three control-plane decisions in :mod:`app.services.autonomous_pentest` are
derived from the TEXT a provider tool returned during the cycle:

- :func:`cycle_made_progress` credits ``manage_openaev_threat_arsenal`` only when
  the result starts with an arsenal MUTATION marker, so a ``list`` / ``get`` read
  cannot reset the convergence guard's stall streak;
- :func:`cycle_asked_for_input` upgrades a missing decision signal to a
  ``waiting_input`` park when the cycle recorded a ``QUESTION`` event or drove the
  run to ``WAITING_INPUT``;
- :func:`cycle_consumed_directive` resets the unanswered-recheck counter when the
  operator actually answered.

Every one of those strings is produced in a DIFFERENT module
(``integrations/providers/openaev/autonomous.py`` and ``.../threat_arsenal.py``)
as LLM-facing prose, and the two sides share no constant. Re-wording the prose
therefore breaks the control plane silently, and always in the dangerous
direction:

- a reworded arsenal marker makes a genuine mutation stop counting as progress,
  so the no-progress guard stops a HEALTHY run that is busy authoring arsenal;
- a reworded QUESTION / WAITING_INPUT string disables the HITL coercion, so the
  adapter keeps cycling against a run that is really waiting on a human - the
  exact stall the coercion exists to prevent;
- a reworded directive line makes a live operator dialogue look unanswered, so a
  plan build FAILS on the unanswered-recheck cap in the middle of a conversation.

Tests that hand-write the expected strings restate the constant instead of
checking it and cannot catch any of the above. These drive the REAL provider
tools (HTTP mocked) and feed their ACTUAL return values into the detectors, so a
reword fails here instead of in production.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.openaev import autonomous as auto_tools
from app.services import autonomous_pentest as ap
from app.tools.base import CallableToolExecutor, ToolCall


class _ToolResult:
    """Tool-result message carrying the provider's real return string."""

    def __init__(self, name: str, content: str) -> None:
        self.role = "tool"
        self.name = name
        self.content = content
        self.tool_calls = None


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _executor(monkeypatch, payload: object = None) -> CallableToolExecutor:
    """Register the real run-coordination tools with the HTTP layer mocked."""

    async def _fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        return _FakeResponse(payload), None

    monkeypatch.setattr(auto_tools, "_api", _fake_api)
    executor = CallableToolExecutor()
    auto_tools.register("https://oaev.example", {"Authorization": "Bearer x"}, executor)
    return executor


async def _call(executor: CallableToolExecutor, name: str, **args) -> _ToolResult:
    result = await executor.execute_tool(ToolCall(id="1", name=name, arguments=args))
    return _ToolResult(name, result.content)


@pytest.mark.asyncio
async def test_real_question_event_result_triggers_hitl_coercion(monkeypatch):
    executor = _executor(monkeypatch)
    message = await _call(
        executor,
        "record_openaev_event",
        run_id="run-1",
        type="QUESTION",
        title="Which targets?",
    )
    assert ap.cycle_asked_for_input([message]) is True


@pytest.mark.asyncio
async def test_real_narration_event_result_does_not_trigger_coercion(monkeypatch):
    # Only a QUESTION is a HITL park signal; narrating must never park the run.
    executor = _executor(monkeypatch)
    message = await _call(
        executor,
        "record_openaev_event",
        run_id="run-1",
        type="NARRATION",
        title="Progress",
    )
    assert ap.cycle_asked_for_input([message]) is False


@pytest.mark.asyncio
async def test_real_waiting_input_status_result_triggers_hitl_coercion(monkeypatch):
    executor = _executor(monkeypatch)
    message = await _call(
        executor,
        "update_openaev_run_status",
        run_id="run-1",
        status="waiting_input",
    )
    assert ap.cycle_asked_for_input([message]) is True


@pytest.mark.asyncio
async def test_real_non_waiting_status_result_does_not_trigger_coercion(monkeypatch):
    executor = _executor(monkeypatch)
    message = await _call(
        executor, "update_openaev_run_status", run_id="run-1", status="running"
    )
    assert ap.cycle_asked_for_input([message]) is False


@pytest.mark.asyncio
async def test_real_delivered_directives_result_counts_as_answered(monkeypatch):
    executor = _executor(
        monkeypatch,
        payload=[
            {
                "autonomous_directive_content": "Focus on the finance subnet",
                "autonomous_directive_created_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    message = await _call(executor, "consume_openaev_directives", run_id="run-1")
    assert ap.cycle_consumed_directive([message]) is True


@pytest.mark.asyncio
async def test_real_empty_directive_queue_result_is_not_an_answer(monkeypatch):
    # The persona calls this EVERY cycle, so the empty-queue sentinel must never
    # read as the operator answering - that would reset the unanswered-recheck
    # counter every cycle and the build cap could never fire.
    executor = _executor(monkeypatch, payload=[])
    message = await _call(executor, "consume_openaev_directives", run_id="run-1")
    assert ap.cycle_consumed_directive([message]) is False


def test_arsenal_call_is_not_attack_path_progress():
    # WS4-C steps-first: manufacturing a library action is not, by itself,
    # advancing the attack path, so NO arsenal result - mutation OR read - is
    # credited as forward progress (an arsenal-only cycle must feed the no-progress
    # guard so the "manufacturing arsenal, not orchestrating a path" stall trips).
    # Strings mirror the provider's real returns in threat_arsenal.py.
    for content in (
        "Created threat arsenal action.\nid: a-1",
        "Updated threat arsenal action.\nid: a-1",
        "Forked threat arsenal action.\nid: a-3",
        "Duplicated threat arsenal action.\nid: a-2",
        "Deleted threat arsenal action a-1.",
        "No threat arsenal actions found.",
    ):
        message = _ToolResult("manage_openaev_threat_arsenal", content)
        assert ap.cycle_made_progress([message]) is False
