"""Fleet documentation generation never fails for its size (#11292), without a database.

Pinned here: an answer cut at its budget is continued where it stopped and the
pieces joined; a whole draft still cut is drafted in parts (sections in halves,
then one section in plain markdown) and every section lands; the output budget
is read from the model in use; a transient provider failure is asked again; a
section the provider keeps failing on is pending, the rest is saved and a
pending section keeps its current text; nothing produced at all names the cause;
and no user-facing error of the generator speaks of a cut answer or tells the
person to work around the tool (the CI guard).
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_docs import DOC_SECTIONS


class _ProviderError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"provider answered {status}")
        self.status_code = status


def _fleet(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "name": "Large Desk",
        "description": "Many members.",
        "is_builtin": False,
        "documentation": None,
    }
    return SimpleNamespace(**{**base, **fields})


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    async def _context(_db, _fleet):
        return {"fleet": {"name": "Large Desk"}, "members": [], "connected": []}

    monkeypatch.setattr(gen, "load_draft_context", _context)
    monkeypatch.setattr(gen, "TRANSIENT_BASE_DELAY", 0.0)


@pytest.fixture
def fingerprint(monkeypatch):
    from app.services import fleet_docs

    async def fake(_db, _fleet):
        return "f" * 16

    monkeypatch.setattr(fleet_docs, "current_fingerprint", fake)


class _Scripted:
    """A model answering by a function of the messages; records every call and its budget."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[tuple[list, int]] = []

    async def chat(self, messages, *, model, temperature, max_tokens):
        self.calls.append((messages, max_tokens))
        result = self.answer(messages, len(self.calls))
        if isinstance(result, BaseException):
            raise result
        content, stop = result if isinstance(result, tuple) else (result, "end_turn")
        return SimpleNamespace(content=content, stop_reason=stop)


def _asked(messages) -> str:
    return messages[1].content


def _wants_parts(messages) -> bool:
    """The call asks for the structured parts alone (#9979), whole or by field."""
    last = messages[-1].content
    return len(messages) > 2 and (last == gen.STRUCTURED_ONLY or last.startswith("Answer with ONE JSON object"))


def _section_of(messages) -> str | None:
    """The one section a plain-markdown call asks for, else None."""
    first = _asked(messages).split("\n", 1)[0]
    if first.startswith("Section to write: "):
        return first.removeprefix("Section to write: ").split(":", 1)[0]
    return None


async def test_a_cut_answer_is_continued_and_the_pieces_joined() -> None:
    answer = json.dumps({k: f"Text of {k}." for k in DOC_SECTIONS})
    head, tail = answer[: len(answer) // 2], answer[len(answer) // 2 :]

    def reply(messages, n):
        if n == 1:
            return head, "max_tokens"
        if messages[-1].content == gen.CONTINUE:
            assert messages[-2].role == "assistant" and messages[-2].content == head
            return tail
        return json.dumps({"structured": {"purpose_line": "Why."}})

    model = _Scripted(reply)
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert draft.sections == {k: f"Text of {k}." for k in DOC_SECTIONS}
    assert draft.pending == []


async def test_a_large_fleet_on_a_small_model_is_drafted_in_parts_and_completes(monkeypatch) -> None:
    """The acceptance case: every grouped answer overflows a 4k model; each section still lands."""
    monkeypatch.setattr(gen, "resolve_provider_max_output_tokens", lambda _llm, _model: 4096)

    def reply(messages, _n):
        if _wants_parts(messages):
            return json.dumps({"structured": {"purpose_line": "Why."}})
        key = _section_of(messages)
        if key:
            return f"The {key} of the fleet."
        if messages[-1].content == gen.CONTINUE or "Sections to write" in _asked(messages):
            return '{"purpose": "a very long answer that never ends', "max_tokens"
        return json.dumps({"structured": {"purpose_line": "Why."}})

    model = _Scripted(reply)
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert draft.sections == {k: f"The {k} of the fleet." for k in DOC_SECTIONS}
    assert draft.pending == []
    assert {budget for _m, budget in model.calls} == {4096}, "the budget is the model's own ceiling"
    assert draft.structured and draft.structured["purpose_line"] == "Why."


def test_the_output_budget_is_read_from_the_model(monkeypatch) -> None:
    monkeypatch.setattr(gen, "resolve_provider_max_output_tokens", lambda _llm, _model: 4096)
    assert gen.output_budget(object(), "small") == 4096
    monkeypatch.setattr(gen, "resolve_provider_max_output_tokens", lambda _llm, _model: 128_000)
    assert gen.output_budget(object(), "large") == gen.DRAFT_BUDGET_MAX
    monkeypatch.setattr(gen, "resolve_provider_max_output_tokens", lambda _llm, _model: None)
    assert gen.output_budget(object(), None) == gen.DRAFT_MAX_TOKENS


async def test_a_transient_provider_error_is_asked_again() -> None:
    answer = json.dumps(
        {"sections": {k: f"Text of {k}." for k in DOC_SECTIONS}, "structured": {"purpose_line": "Why."}}
    )

    def reply(_messages, n):
        return _ProviderError(503) if n == 1 else answer

    model = _Scripted(reply)
    draft = await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    assert list(draft.sections) == list(DOC_SECTIONS)
    assert len(model.calls) == 2


def test_transient_failures_are_told_from_the_others() -> None:
    assert gen.is_transient(_ProviderError(429))
    assert gen.is_transient(_ProviderError(502))
    assert gen.is_transient(TimeoutError())
    assert not gen.is_transient(_ProviderError(400))
    assert not gen.is_transient(ValueError("a defect"))


def _told_not_to_retry(cls, status: int):
    import httpx

    request = httpx.Request("POST", "https://gateway.local/v1/chat/completions")
    response = httpx.Response(status, headers={"x-should-retry": "false", "retry-after": "86400"}, request=request)
    return cls("quota exceeded", response=response, body=None)


def test_an_error_the_provider_says_not_to_retry_is_not_transient() -> None:
    import openai

    assert not gen.is_transient(_told_not_to_retry(openai.RateLimitError, 429))
    assert not gen.is_transient(_told_not_to_retry(openai.InternalServerError, 502))


async def test_an_error_the_provider_says_not_to_retry_is_never_sent_twice(monkeypatch) -> None:
    import openai

    from app.llm import base

    # No real back-off: a wrong retry fails on the call count, not after minutes of waits.
    monkeypatch.setattr(base, "_extract_retry_after", lambda _exc: None)
    model = _Scripted(lambda _m, _n: _told_not_to_retry(openai.RateLimitError, 429))
    with pytest.raises(gen.FleetDocDraftError):
        await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    # The drafter still tries its parts and sections, each once.
    sent = [tuple(message.content for message in messages) for messages, _budget in model.calls]
    assert len(sent) == len(set(sent))


async def test_a_section_the_provider_keeps_failing_on_is_pending_and_the_rest_is_saved(fingerprint) -> None:
    """One section failing after its retries: the rest is saved, that section keeps its text and is pending."""

    def reply(messages, _n):
        if _wants_parts(messages):
            return json.dumps({"structured": {"purpose_line": "Why."}})
        key = _section_of(messages)
        if key == "flow":
            return _ProviderError(500)
        if key:
            return f"New {key}."
        if "Sections to write" in _asked(messages) or messages[-1].content == gen.CONTINUE:
            return "{not json", "end_turn"
        return json.dumps({"structured": {"purpose_line": "Why."}})

    fleet = _fleet(documentation={"sections": {"purpose": "Old purpose.", "flow": "Old flow."}, "version": 3})
    draft = await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Scripted(reply))
    assert draft.pending == ["flow"]
    saved = fleet.documentation["sections"]
    assert saved["flow"] == "Old flow.", "a pending section keeps the text it had"
    assert saved["purpose"] == "New purpose."
    assert set(saved) == set(DOC_SECTIONS)
    assert fleet.documentation["version"] == 4


async def test_a_first_generation_saves_without_the_pending_section(fingerprint) -> None:
    def reply(messages, _n):
        if _wants_parts(messages):
            return json.dumps({"structured": {"purpose_line": "Why."}})
        key = _section_of(messages)
        if key == "triggers":
            return _ProviderError(502)
        if key:
            return f"New {key}."
        if "Sections to write" in _asked(messages):
            return _ProviderError(503)
        return json.dumps({"structured": {"purpose_line": "Why."}})

    fleet = _fleet()
    draft = await gen.regenerate_fleet_documentation(
        object(), uuid.uuid4(), fleet, base_version=0, llm=_Scripted(reply)
    )
    assert draft.pending == ["triggers"]
    assert set(fleet.documentation["sections"]) == set(DOC_SECTIONS) - {"triggers"}


async def test_nothing_produced_at_all_names_the_cause_in_one_line() -> None:
    model = _Scripted(lambda _m, _n: _ProviderError(503))
    with pytest.raises(gen.FleetDocDraftError) as failed:
        await gen.draft_fleet_documentation(object(), uuid.uuid4(), _fleet(), llm=model)
    message = str(failed.value)
    assert message.startswith("The AI model did not answer:")
    assert "\n" not in message and "section at a time" not in message


async def test_a_pending_section_is_filled_in_the_background(monkeypatch) -> None:
    from app.services import fleet_doc_on_create as on_create

    filled: list[str] = []

    async def fake_fill(_fleet_id, _user_id, sections):
        filled.extend(sections)

    monkeypatch.setattr(on_create, "_fill_pending", fake_fill)
    assert on_create.schedule_pending_sections(uuid.uuid4(), uuid.uuid4(), ["flow", "flow"]) is True
    assert on_create.schedule_pending_sections(uuid.uuid4(), uuid.uuid4(), []) is False
    for task in list(on_create._tasks):
        await task
    assert filled == ["flow"]


def _raised_messages(path: Path) -> list[str]:
    """The literal text of every ``FleetDocDraftError(...)`` raised in *path*."""
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FleetDocDraftError":
            for arg in node.args:
                for part in ast.walk(arg):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        out.append(part.value)
    return out


def test_ci_guard_no_user_error_speaks_of_a_cut_or_a_workaround() -> None:
    """A length stop can never surface as a user error (#11292): no raised message names a cut answer."""
    messages = _raised_messages(Path(gen.__file__))
    assert messages, "the guard reads the generator's raised messages"
    for text in messages:
        lowered = text.lower()
        assert "cut" not in lowered and "too long" not in lowered, text
        assert "at a time" not in lowered and "regenerate one" not in lowered, text
