"""Generate a custom tool's code with the caller's model.

The model is told the runtime contract the executor applies (the identifiers
it binds, ``$files``, the blocked modules, the sandbox's network), the reply
is unwrapped and syntax-checked, and a parser error is handed back once. The
provider is a fake replaying scripted answers - no model is ever called.
"""

from __future__ import annotations

import ast
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.base import ChatResponse
from app.services import custom_tool_code_generation as ccg
from app.services._sandbox_utils import BLOCKED_MODULES

CUT = "max_tokens"
DONE = "end_turn"


class _Script:
    """A provider replaying ``(content, stop_reason)`` answers, recording calls."""

    provider_name = "script"

    def __init__(self, *answers: tuple[str, str]) -> None:
        self._answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        self.calls.append({"messages": messages, **kwargs})
        content, stop_reason = self._answers.pop(0)
        return ChatResponse(content=content, model="m", stop_reason=stop_reason, tool_calls=None, usage={})

    @property
    def budgets(self) -> list[int]:
        return [call["max_tokens"] for call in self.calls]


class _Broken:
    provider_name = "broken"

    async def chat(self, messages=None, **kwargs):
        raise ConnectionError("provider down")


@pytest.fixture(autouse=True)
def _custom_tools_on_docker(monkeypatch):
    from app.core.config import settings
    from app.services import custom_tool_executor

    async def _sandbox() -> str:
        return "docker"

    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr(custom_tool_executor, "get_effective_sandbox", _sandbox)


def _messages(language: str, *params: ccg.ToolParameter, **kwargs) -> tuple[str, str]:
    defaults = {
        "brief": "Look up a thing",
        "tool_name": "",
        "tool_description": "",
        "current_code": None,
        "sandbox": "docker",
        "allow_list": False,
        "blocked": BLOCKED_MODULES,
    }
    messages = ccg.build_messages(language=language, parameters=params, **{**defaults, **kwargs})
    return messages[0].content, messages[1].content


# ---------------------------------------------------------------------------
# The contract the model is told
# ---------------------------------------------------------------------------


def test_inputs_are_named_as_the_executor_binds_them():
    params = (
        ccg.ToolParameter("my-query", "string", "the search text", True),
        ccg.ToolParameter("class", "integer"),
        ccg.ToolParameter("2fa"),
    )

    _, js = _messages("javascript", *params)
    assert "`$my_query` (string, required) - parameter \"my-query\": the search text" in js
    assert "`$class` (integer, optional)" in js
    assert "`$_files` (array, always passed" in js

    _, py = _messages("python", *params)
    assert "`my_query` (string, required)" in py
    assert "`class_` (integer, optional)" in py
    assert "`_2fa` (string, optional)" in py
    assert "`_files` (array, always passed" in py


def test_bindings_follow_the_executor_in_declared_order():
    from app.services.custom_tool_executor import _build_js_script, _build_python_script

    names = ["a-b", "a_b", "_xtm_json", "$files"]
    inputs = dict.fromkeys(names, 1)
    js = _build_js_script("", inputs)
    py = _build_python_script("", inputs)
    for binding in ccg.input_bindings("javascript", names):
        assert f"const {binding.identifier} = 1;" in js
    for binding in ccg.input_bindings("python", names):
        assert f"{binding.identifier} = __xtm_tool_inputs__[" in py


def test_inputs_that_collide_once_sanitised_are_flagged():
    params = (ccg.ToolParameter("a-b"), ccg.ToolParameter("a_b"), ccg.ToolParameter("other"))

    _, py = _messages("python", *params)
    assert '`__xtm_tool_inputs__["a-b"]`' in py
    assert '`__xtm_tool_inputs__["a_b"]`' in py
    assert "`other` (string, optional)\n" in py

    _, js = _messages("javascript", *params)
    assert js.count("ask the user to rename one of them") == 2


def test_the_key_of_a_colliding_input_is_a_python_string_literal():
    name = 'a"b\\c'
    _, py = _messages("python", ccg.ToolParameter(name), ccg.ToolParameter("a_b_c"))
    expression = py.split("read this one as `", 1)[1].split("`)", 1)[0]
    subscript = ast.parse(expression, mode="eval").body
    assert isinstance(subscript, ast.Subscript)
    assert ast.literal_eval(subscript.slice) == name


def test_a_parameter_named_files_does_not_hide_the_attached_files():
    _, js = _messages("javascript", ccg.ToolParameter("$files", "array"), ccg.ToolParameter("q"))
    assert js.count("`$_files`") == 1
    assert "always passed" in js


def test_the_runtime_rules_differ_per_language():
    js_system, _ = _messages("javascript")
    assert "top-level `await` and `return` are allowed" in js_system
    assert "console.log" in js_system
    assert 'typeof $name === "undefined"' in js_system

    py_system, _ = _messages("python")
    assert "`return` is not allowed" in py_system
    assert "__xtm_tool_inputs__.get" in py_system
    assert "subprocess" in py_system


def test_the_blocked_modules_are_the_effective_ones():
    blocked = BLOCKED_MODULES - {"requests"}
    py_system, _ = _messages("python", blocked=blocked)
    listed = py_system.split("these modules cannot be imported: ", 1)[1].split(".\n", 1)[0].split(", ")
    assert set(listed) == set(blocked)


def test_the_modules_an_administrator_allowed_are_offered():
    py_system, _ = _messages("python")
    assert "- Standard library only, and these modules" in py_system

    py_system, _ = _messages("python", blocked=BLOCKED_MODULES - {"requests", "httpx", "urllib"})
    assert "- Standard library plus the modules an administrator allowed (httpx, requests), and" in py_system


@pytest.mark.parametrize(
    ("language", "sandbox", "allow_list", "blocked", "expected"),
    [
        ("javascript", "docker", True, BLOCKED_MODULES, "no network access"),
        ("python", "docker_unavailable", True, frozenset(), "no network access"),
        ("javascript", "opensandbox", False, BLOCKED_MODULES, "no network access"),
        ("javascript", "opensandbox", True, BLOCKED_MODULES, "only the hosts an administrator allowed"),
        ("javascript", "subprocess", False, BLOCKED_MODULES, "`fetch` can reach HTTP APIs"),
        ("python", "subprocess", False, BLOCKED_MODULES, "No HTTP module can be imported"),
        ("python", "subprocess", False, BLOCKED_MODULES - {"urllib"}, "HTTP calls can be made with urllib."),
        (
            "python",
            "opensandbox",
            True,
            BLOCKED_MODULES - {"httpx"},
            "made with httpx, reaching only the hosts an administrator allowed",
        ),
    ],
)
def test_the_network_sentence_follows_the_sandbox(language, sandbox, allow_list, blocked, expected):
    assert expected in ccg.network_note(language, sandbox, allow_list=allow_list, blocked=blocked)


def test_the_request_carries_the_tool_and_the_current_code():
    _, user = _messages(
        "python",
        tool_name="Weather",
        tool_description="Current weather",
        current_code="print(1)",
        brief="Add a unit switch",
    )
    assert "Tool name: Weather" in user
    assert "Tool description: Current weather" in user
    assert "Request:\nAdd a unit switch" in user
    assert user.endswith("Current code (rework it to fulfil the request):\nprint(1)")


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ("print(1)", "print(1)"),
        ("```python\nprint(1)\n```", "print(1)"),
        ("```\nprint(1)\n", "print(1)"),
        ("Here is the code:\n```js\nconsole.log(1);\n```\nIt logs 1.", "console.log(1);"),
        ("```js\na()\n```\nand\n```js\nb()\n```", "```js\na()\n```\nand\n```js\nb()\n```"),
        ("const s = 'x ``` y';", "const s = 'x ``` y';"),
    ],
)
def test_a_fence_around_the_code_is_dropped(reply, code):
    assert ccg.unwrap_code(reply) == code


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["x = 1\nreturn x", "await f()"])
async def test_the_python_check_refuses_what_the_runtime_cannot_run(code):
    """The script runs at module top level, where the compiler refuses a
    ``return`` or an ``await`` that ``ast.parse`` alone accepts."""
    from app.services.custom_tool_executor import lint_tool_code

    verdict = await lint_tool_code("python", code)

    assert verdict["status"] == "error"
    assert verdict["line"] == code.count("\n") + 1


async def test_the_python_check_compiles_the_script_the_runtime_runs():
    """The runtime puts its header and input bindings before the code, so a
    ``from __future__`` import that compiles on its own cannot run."""
    from app.services.custom_tool_executor import lint_tool_code

    verdict = await lint_tool_code("python", "from __future__ import annotations\nprint(1)")
    assert verdict["status"] == "error"
    assert verdict["line"] == 1
    assert "from __future__ imports cannot be used" in verdict["message"]

    verdict = await lint_tool_code("python", "x = 1\n\ndef f(:\n    pass")
    assert verdict == {"status": "error", "message": verdict["message"], "line": 3}


async def test_a_parsing_answer_is_returned_without_a_repair():
    llm = _Script(("```python\nprint('ok')\n```", DONE))

    result = await ccg.generate_tool_code(llm, None, language="python", brief="Say ok")

    assert result.code == "print('ok')"
    assert result.syntax_ok and not result.repaired and not result.truncated
    assert len(llm.calls) == 1
    assert llm.calls[0]["temperature"] == 0.2


async def test_a_parser_error_is_handed_back_once_with_its_line():
    llm = _Script(("x = 1\nreturn x", DONE), ("x = 1\nprint(x)", DONE))

    result = await ccg.generate_tool_code(llm, None, language="python", brief="Print one")

    assert result.code == "x = 1\nprint(x)"
    assert result.syntax_ok and result.repaired
    repair = llm.calls[1]["messages"]
    assert repair[-2].role == "assistant" and repair[-2].content == "x = 1\nreturn x"
    assert repair[-1].content.startswith("The syntax check failed at line 2:")


async def test_a_repair_that_still_fails_is_returned_with_its_verdict():
    llm = _Script(("def f(:\n  pass", DONE), ("def g(:\n  pass", DONE))

    result = await ccg.generate_tool_code(llm, None, language="python", brief="Anything")

    assert result.code == "def g(:\n  pass"
    assert result.repaired and not result.syntax_ok
    assert result.syntax_error and result.syntax_error_line == 1
    assert len(llm.calls) == 2


async def test_a_check_error_without_a_line_is_not_handed_back(monkeypatch):
    async def _no_node(language, code):
        return {"status": "error", "message": "Node.js is not installed on this server"}

    monkeypatch.setattr(ccg, "_lint", _no_node)
    llm = _Script(("console.log(1);", DONE))

    result = await ccg.generate_tool_code(llm, None, language="javascript", brief="Log one")

    assert len(llm.calls) == 1
    assert not result.syntax_ok and result.syntax_error_line is None
    assert result.syntax_error == "Node.js is not installed on this server"


async def test_an_answer_still_cut_is_returned_flagged():
    llm = _Script(("print(1)\nprint(2", CUT), ("print(1)\nprint(2)", CUT))

    result = await ccg.generate_tool_code(llm, None, language="python", brief="Two prints")

    assert result.truncated and result.syntax_ok
    assert llm.budgets == [ccg.CODE_MAX_TOKENS, 2 * ccg.CODE_MAX_TOKENS]


async def test_an_empty_answer_is_refused():
    with pytest.raises(ccg.CodeGenerationError, match="no code"):
        await ccg.generate_tool_code(_Script(("```\n```", DONE)), None, language="python", brief="x")


async def test_a_provider_failure_is_refused_with_a_plain_sentence():
    with pytest.raises(ccg.CodeGenerationError) as exc:
        await ccg.generate_tool_code(_Broken(), None, language="python", brief="x")
    assert "provider down" not in str(exc.value)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _client(monkeypatch, llm):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.api.v1 import custom_tools
    from app.core.database import get_db

    async def _llm_for_user(*_args, **_kwargs):
        return llm, None

    app = FastAPI()
    app.include_router(custom_tools.router, prefix="/api/v1/custom-tools")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[get_db] = lambda: SimpleNamespace(in_transaction=lambda: False)
    monkeypatch.setattr(custom_tools, "get_llm_for_user", _llm_for_user)
    return TestClient(app, raise_server_exceptions=False)


def test_the_route_returns_the_code_and_its_verdict(monkeypatch):
    llm = _Script(("print(query.upper())", DONE))

    resp = _client(monkeypatch, llm).post(
        "/api/v1/custom-tools/generate-code",
        json={
            "description": "Upper-case the query",
            "language": "python",
            "tool_name": "Shout",
            "parameters": [{"name": "query", "type": "string", "required": True}],
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "code": "print(query.upper())",
        "truncated": False,
        "syntax_ok": True,
        "syntax_error": None,
        "syntax_error_line": None,
        "repaired": False,
    }
    assert "`query` (string, required)" in llm.calls[0]["messages"][1].content


def test_a_long_saved_description_is_clipped_never_refused(monkeypatch):
    llm = _Script(("print(query)", DONE))

    resp = _client(monkeypatch, llm).post(
        "/api/v1/custom-tools/generate-code",
        json={
            "description": "Echo the query",
            "language": "python",
            "tool_description": "T" * (ccg.TOOL_DESCRIPTION_MAX_CHARS + 500),
            "parameters": [{"name": "query", "description": "P" * (ccg.PARAMETER_DESCRIPTION_MAX_CHARS + 500)}],
        },
    )

    assert resp.status_code == 200, resp.text
    prompt = llm.calls[0]["messages"][1].content
    assert "T" * ccg.TOOL_DESCRIPTION_MAX_CHARS in prompt
    assert "T" * (ccg.TOOL_DESCRIPTION_MAX_CHARS + 1) not in prompt
    assert "P" * ccg.PARAMETER_DESCRIPTION_MAX_CHARS in prompt
    assert "P" * (ccg.PARAMETER_DESCRIPTION_MAX_CHARS + 1) not in prompt


@pytest.mark.parametrize(
    "body",
    [
        {"description": "x", "language": "ruby"},
        {"description": "   ", "language": "python"},
        {"description": "x", "language": "python", "parameters": [{"name": ""}]},
    ],
)
def test_the_route_refuses_a_malformed_request(monkeypatch, body):
    llm = _Script()
    resp = _client(monkeypatch, llm).post("/api/v1/custom-tools/generate-code", json=body)
    assert resp.status_code == 422
    assert llm.calls == []


def test_the_route_is_refused_while_custom_tools_are_disabled(monkeypatch):
    from app.api.v1 import custom_tools

    llm = _Script()
    client = _client(monkeypatch, llm)
    monkeypatch.setattr(custom_tools, "security_option", lambda key: key == "disable_custom_tools")

    resp = client.post("/api/v1/custom-tools/generate-code", json={"description": "x", "language": "python"})

    assert resp.status_code == 403
    assert llm.calls == []


def test_a_model_failure_is_a_502(monkeypatch):
    resp = _client(monkeypatch, _Broken()).post(
        "/api/v1/custom-tools/generate-code", json={"description": "x", "language": "python"}
    )
    assert resp.status_code == 502
    assert "provider down" not in resp.text
