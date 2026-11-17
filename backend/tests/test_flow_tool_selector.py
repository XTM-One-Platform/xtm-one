"""Hermetic tests for LLM-mode tool selection on the Call-tool node.

No database and no provider: the LLM is a scripted fake, so what is under test
is the selector's own contract — that it offers only the declared candidates,
refuses a name outside them, corrects missing arguments, and turns every
failure into a returned error instead of an exception.

Candidate RESOLUTION — which tools a node may offer, and the visibility filter
that bounds it — is covered against real PostgreSQL in
``test_flow_runner_postgres.py``. The walker's own ``_exec_call_tool_llm``
wiring (render, resolve, select, invoke) has no test of its own yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.services.flow_llm import LlmSettings
from app.services.flow_tool_selector import select_tool
from app.tools.base import ToolCall, ToolDefinition

# ── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeResponse:
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass
class _FakeLLM:
    """Replays a scripted list of responses, recording what it was offered."""

    responses: list[_FakeResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("the selector called the model once too often")
        return self.responses.pop(0)


def _patch_llm(monkeypatch, llm, *, model: str | None = "test-model") -> None:
    async def _resolve(db, settings, *, user_id, fallback_model=None, fallback_provider_id=None):
        _patch_llm.last_settings = settings
        return llm, model

    async def _routing(db):
        return None, None

    monkeypatch.setattr(
        "app.services.flow_tool_selector.resolve_llm", _resolve
    )
    monkeypatch.setattr(
        "app.services.flow_tool_selector.routing_model", _routing
    )


def _tool(name: str, *, required: list[str] | None = None) -> ToolDefinition:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }
    if required:
        schema["required"] = required
    return ToolDefinition(name=name, description=f"The {name} tool", input_schema=schema)


# ── Happy path ───────────────────────────────────────────────────────────────


async def test_the_model_picks_one_of_the_declared_tools(monkeypatch):
    llm = _FakeLLM(
        [
            _FakeResponse(
                content="web_fetch reaches the page.",
                tool_calls=[
                    ToolCall(id="1", name="web_fetch", arguments={"query": "x"})
                ],
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch"), _tool("stix_bundle_validator")],
    )

    assert selection.ok
    assert selection.tool_name == "web_fetch"
    assert selection.arguments == {"query": "x"}
    assert selection.attempts == 1
    # The candidates' real definitions are what the provider was offered — the
    # selection is structurally constrained, not parsed out of prose.
    offered = llm.calls[0]["tools"]
    assert [d.name for d in offered] == ["web_fetch", "stix_bundle_validator"]


async def test_a_name_outside_the_candidates_is_refused_then_corrected(monkeypatch):
    """A hallucinated tool name must never be executed — it is fed back."""
    llm = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[ToolCall(id="1", name="rm_rf", arguments={})]
            ),
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="2", name="web_fetch", arguments={"query": "x"})
                ]
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        max_attempts=2,
    )

    assert selection.ok
    assert selection.tool_name == "web_fetch"
    assert selection.attempts == 2
    # The correction names the offered set rather than just rejecting.
    correction = llm.calls[1]["messages"][-1].content
    assert "not one of the offered tools" in correction
    assert "web_fetch" in correction


async def test_a_missing_required_argument_is_fed_back(monkeypatch):
    llm = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[ToolCall(id="1", name="search", arguments={})]
            ),
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="2", name="search", arguments={"query": "acme"})
                ]
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Find acme",
        definitions=[_tool("search", required=["query"])],
        max_attempts=2,
    )

    assert selection.ok
    assert selection.arguments == {"query": "acme"}
    assert "`query`" in llm.calls[1]["messages"][-1].content


# ── Failure modes: an error, never an exception ──────────────────────────────


async def test_prose_instead_of_a_tool_call_fails_the_node(monkeypatch):
    llm = _FakeLLM(
        [
            _FakeResponse(content="I think you should fetch it."),
            _FakeResponse(content="Really, fetch it."),
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        max_attempts=2,
    )

    assert not selection.ok
    assert selection.tool_name is None
    assert "did not produce a usable tool call" in selection.error


async def test_an_unreachable_provider_names_the_pinned_provider(monkeypatch):
    async def _resolve(db, settings, *, user_id, fallback_model=None, fallback_provider_id=None):
        raise RuntimeError("no credentials")

    async def _routing(db):
        return None, None

    monkeypatch.setattr("app.services.flow_tool_selector.resolve_llm", _resolve)
    monkeypatch.setattr("app.services.flow_tool_selector.routing_model", _routing)

    provider_id = "11111111-1111-1111-1111-111111111111"
    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        llm_config={"provider": provider_id},
    )

    assert not selection.ok
    assert provider_id in selection.error
    assert "no credentials" in selection.error


async def test_no_candidates_is_refused_before_any_model_call():
    selection = await select_tool(
        None, objective="Do the thing", definitions=[]
    )
    assert not selection.ok
    assert "No candidate tools" in selection.error


async def test_an_empty_objective_is_refused():
    selection = await select_tool(
        None, objective="   ", definitions=[_tool("web_fetch")]
    )
    assert not selection.ok
    assert "No objective" in selection.error


async def test_extra_tool_calls_are_dropped_and_said_so(monkeypatch):
    """One node is one decision; a second proposed call is reported, not run."""
    llm = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="1", name="web_fetch", arguments={"query": "a"}),
                    ToolCall(id="2", name="web_fetch", arguments={"query": "b"}),
                ]
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None, objective="Fetch", definitions=[_tool("web_fetch")]
    )

    assert selection.ok
    assert selection.arguments == {"query": "a"}
    assert "2 calls" in selection.reason


# ── LLM node settings ────────────────────────────────────────────────────────


def test_settings_parse_defensively():
    """Config travels in a JSON document; nothing in it may raise."""
    settings = LlmSettings.from_node_config(
        {
            "provider": "not-a-uuid",
            "model": "  gpt-5  ",
            "temperature": 9.9,
            "max_tokens": "512",
            "reasoning_effort": "banana",
            "instructions": "   ",
        }
    )
    # An unreadable provider degrades to the platform default rather than
    # failing the step.
    assert settings.provider_id is None
    assert settings.model == "gpt-5"
    # Clamped to the catalog's slider bounds.
    assert settings.temperature == 2.0
    # A string is not a number: max_tokens stays unset rather than guessing.
    assert settings.max_tokens is None
    assert settings.reasoning_effort is None
    assert settings.instructions is None


def test_settings_default_max_tokens_is_the_callers_own():
    """A yes/no condition keeps its cheap budget when the node sets nothing."""
    assert LlmSettings().chat_kwargs(default_max_tokens=10) == {"max_tokens": 10}
    pinned = LlmSettings(max_tokens=256, temperature=0.0, reasoning_effort="low")
    assert pinned.chat_kwargs(default_max_tokens=10) == {
        "max_tokens": 256,
        "temperature": 0.0,
        "reasoning_effort": "low",
    }


@pytest.mark.parametrize("provider", ["", None, "   "])
def test_an_unset_provider_means_inherit(provider):
    assert LlmSettings.from_node_config({"provider": provider}).provider_id is None


# ── Provider message-shape invariants ────────────────────────────────────────
#
# The fake accepts any message list, so these assert the invariants a real
# provider enforces. The one that bit: an assistant message with empty content
# is rejected outright by Anthropic on any non-final turn, which made every
# corrective retry fail on the default provider and report itself as "the
# model could not be reached".


def _assert_no_empty_messages(llm: _FakeLLM) -> None:
    for call in llm.calls:
        for message in call["messages"]:
            assert (message.content or "").strip(), (
                f"a {message.role} message with empty content was sent to the "
                "provider — Anthropic 400s on this"
            )


async def test_the_retry_never_sends_an_empty_assistant_message(monkeypatch):
    """The corrected answer is a tool call with NO prose — the common case."""
    llm = _FakeLLM(
        [
            # No `content`: exactly what a tool-calling model returns.
            _FakeResponse(
                tool_calls=[ToolCall(id="1", name="rm_rf", arguments={})]
            ),
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="2", name="web_fetch", arguments={"query": "x"})
                ]
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        max_attempts=2,
    )

    assert selection.ok
    _assert_no_empty_messages(llm)
    # The correction still reached the model, as a user turn.
    assert llm.calls[1]["messages"][-1].role == "user"
    assert "not one of the offered tools" in llm.calls[1]["messages"][-1].content
    # And no assistant turn was fabricated for an answer that had no prose.
    assert [m.role for m in llm.calls[1]["messages"]] == [
        "system",
        "user",
        "user",
    ]


async def test_prose_is_echoed_back_when_the_model_wrote_some(monkeypatch):
    """When there IS prose it belongs in the transcript — that is context the
    correction refers to."""
    llm = _FakeLLM(
        [
            _FakeResponse(
                content="I'll use rm_rf.",
                tool_calls=[ToolCall(id="1", name="rm_rf", arguments={})],
            ),
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="2", name="web_fetch", arguments={"query": "x"})
                ]
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        max_attempts=2,
    )

    assert selection.ok
    _assert_no_empty_messages(llm)
    roles = [m.role for m in llm.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    # The refused tool_calls are NOT echoed: a `tool_use` block with no
    # matching `tool_result` is rejected for the same reason, and there is no
    # result to give — the call was refused, not executed.
    assistant = llm.calls[1]["messages"][2]
    assert not getattr(assistant, "tool_calls", None)


async def test_no_empty_message_on_the_prose_only_failure_path(monkeypatch):
    """Prose with no tool call also retries — same invariant."""
    llm = _FakeLLM(
        [
            _FakeResponse(content="I think you should fetch it."),
            _FakeResponse(content="Really, fetch it."),
        ]
    )
    _patch_llm(monkeypatch, llm)

    await select_tool(
        None,
        objective="Fetch the advisory",
        definitions=[_tool("web_fetch")],
        max_attempts=2,
    )
    _assert_no_empty_messages(llm)


# ── Argument presence ────────────────────────────────────────────────────────


async def test_an_empty_string_satisfies_a_required_argument(monkeypatch):
    """`""` is a VALUE. Whether it is acceptable is the schema's business
    (`minLength`) — treating it as missing burns a retry and then fails a node
    whose required argument legitimately accepts it."""
    llm = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="1", name="search", arguments={"query": ""})
                ]
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Search for everything",
        definitions=[_tool("search", required=["query"])],
        max_attempts=2,
    )

    assert selection.ok
    assert selection.arguments == {"query": ""}
    # One call: no retry was burned on a value that was there all along.
    assert selection.attempts == 1
    assert len(llm.calls) == 1


# ── Attempt accounting ───────────────────────────────────────────────────────


async def test_attempts_reports_calls_made_not_the_budget(monkeypatch):
    """`attempts` is telemetry; a failure must not overstate the work done."""
    llm = _FakeLLM([_FakeResponse(content="no tool for me")])
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch it",
        definitions=[_tool("web_fetch")],
        max_attempts=1,
    )

    assert not selection.ok
    assert selection.attempts == 1
    assert "after 1 attempt" in selection.error


async def test_a_zero_attempt_budget_still_makes_one_call(monkeypatch):
    """Clamped to 1 rather than looping zero times and reporting a failure no
    model was ever asked about."""
    llm = _FakeLLM(
        [
            _FakeResponse(
                tool_calls=[
                    ToolCall(id="1", name="web_fetch", arguments={"query": "x"})
                ]
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    selection = await select_tool(
        None,
        objective="Fetch it",
        definitions=[_tool("web_fetch")],
        max_attempts=0,
    )

    assert selection.ok
    assert len(llm.calls) == 1
