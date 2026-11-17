# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Draft or improve a custom tool's code with the caller's LLM.

The "Generate with AI" bar of the custom tool editor. The model is told the
runtime contract as the executor applies it, never a paraphrase of it:

* every declared input under the identifier the executor binds it to
  (``_safe_js_identifier`` / ``_safe_python_identifier``, in declared order,
  after the platform's own bindings), plus the attached files the platform
  always passes (``$files``, appended last);
* that an input the caller omits is not declared at all, so an optional one
  is read defensively;
* the Python modules the sandbox refuses (the admin allow-list applied) and
  the network the effective sandbox gives;
* that a secret belongs in a platform variable, never in the code - and that
  an invented ``{{var_id.<uuid>}}`` placeholder makes the tool refuse to run.

The answer is unwrapped (a markdown fence around it is dropped) and checked
by ``lint_tool_code``, a syntax check that never runs the code. A parser
error is handed back to the model once, with its line; whatever comes back is
returned with its verdict, so the editor says whether it parses. Nothing is
saved or executed here: the code lands in the editor for the person to
review, test and save.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.services._sandbox_utils import BLOCKED_MODULES, effective_blocked_modules

logger = logging.getLogger(__name__)

LANGUAGES = ("javascript", "python")
BRIEF_MAX_CHARS = 4_000
#: How much of the tool's and a parameter's description is read: they are
#: context, stored without a bound, so a longer one is clipped, never refused.
TOOL_DESCRIPTION_MAX_CHARS = 4_000
PARAMETER_DESCRIPTION_MAX_CHARS = 2_000
CURRENT_CODE_MAX_CHARS = 60_000
MAX_PARAMETERS = 50
CODE_MAX_TOKENS = 4_096

#: The input every run receives on top of the declared ones (``loaders.py``
#: and the Test route), bound after them.
FILES_INPUT = "$files"
#: Names the Python script binds before the inputs (``_build_python_script``).
_PYTHON_RESERVED = frozenset({"_xtm_json", "__xtm_tool_inputs__"})
_PYTHON_HTTP_MODULES = ("urllib", "http", "requests", "httpx", "aiohttp")

_FENCE_LINE = re.compile(r"^```[\w+-]*[ \t]*$")
_FENCED_BLOCK = re.compile(r"^```[\w+-]*[ \t]*\n(.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL)


class CodeGenerationError(Exception):
    """The model did not return usable code."""


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class InputBinding:
    """What the code reads for one input."""

    name: str
    identifier: str
    #: Another input sanitises to the same base identifier, so which one gets
    #: the suffix depends on the order of the call's arguments.
    ambiguous: bool


@dataclass(frozen=True)
class GeneratedCode:
    code: str
    truncated: bool
    syntax_ok: bool
    syntax_error: str | None = None
    syntax_error_line: int | None = None
    repaired: bool = False


def input_bindings(language: str, names: Sequence[str]) -> list[InputBinding]:
    """The identifier of each input, *names* in the order the runtime binds them."""
    from app.services.custom_tool_executor import _safe_js_identifier, _safe_python_identifier

    if language == "python":
        bind, used, prefix = _safe_python_identifier, set(_PYTHON_RESERVED), ""
    else:
        bind, used, prefix = _safe_js_identifier, set(), "$"
    bases = Counter(bind(name, set()) for name in names)
    return [
        InputBinding(
            name=name,
            identifier=prefix + bind(name, used),
            ambiguous=bases[bind(name, set())] > 1,
        )
        for name in names
    ]


def unwrap_code(text: str | None) -> str:
    """The code in a reply: a fence around the whole reply, or the one fenced
    block a reply wraps in prose, is dropped; anything else is kept as is."""
    text = (text or "").strip()
    lines = text.split("\n")
    fences = [i for i, line in enumerate(lines) if _FENCE_LINE.match(line.strip())]
    if fences and fences[0] == 0 and (fences == [0] or fences == [0, len(lines) - 1]):
        return "\n".join(lines[1 : fences[-1] if len(fences) == 2 else None]).strip("\n")
    blocks = _FENCED_BLOCK.findall(text)
    if len(blocks) == 1:
        return blocks[0].strip("\n")
    return text


def network_note(language: str, sandbox: str, *, allow_list: bool, blocked: frozenset[str]) -> str:
    """What the code can reach on the network in the *sandbox* custom tools run in."""
    no_network = "The sandbox has no network access: an HTTP call fails, so keep to computing over the inputs."
    if sandbox in ("docker", "docker_unavailable"):
        return no_network
    limited = sandbox in ("opensandbox", "opensandbox_unavailable")
    if limited and not allow_list:
        return no_network
    if language == "javascript":
        reach = "only the hosts an administrator allowed" if limited else "HTTP APIs"
        return f"The global `fetch` can reach {reach}."
    importable = [m for m in _PYTHON_HTTP_MODULES if m not in blocked]
    if not importable:
        return (
            "No HTTP module can be imported (" + ", ".join(_PYTHON_HTTP_MODULES) + " are blocked), "
            "so the code cannot call the network: keep to computing over the inputs."
        )
    reach = ", reaching only the hosts an administrator allowed" if limited else ""
    return "HTTP calls can be made with " + ", ".join(importable) + reach + "."


def _parameter_line(param: ToolParameter | None, binding: InputBinding, language: str) -> str:
    if param is None:
        return (
            f"- `{binding.identifier}` (array, always passed, often empty): files attached to the call, each "
            "{file_id, filename, content_type, size} with `content` (the text, for text and office files) "
            "and/or `data_base64` (the bytes, for binary files)."
        )
    kind = "required" if param.required else "optional"
    line = f"- `{binding.identifier}` ({param.type or 'string'}, {kind})"
    if binding.identifier.lstrip("$") != param.name:
        line += f' - parameter "{param.name}"'
    if param.description:
        line += f": {param.description}"
    if binding.ambiguous:
        if language == "python":
            key = json.dumps(param.name, ensure_ascii=False)
            line += f" (another parameter maps to the same name: read this one as `__xtm_tool_inputs__[{key}]`)"
        else:
            line += (
                " (another parameter maps to the same name, so which one is suffixed depends on the call: "
                "say so in a comment on the first line and ask the user to rename one of them)"
            )
    return line


def _runtime_rules(language: str, network: str, blocked: frozenset[str]) -> str:
    if language == "python":
        allowed = sorted(BLOCKED_MODULES - blocked - sys.stdlib_module_names)
        libraries = "Standard library only"
        if allowed:
            libraries = "Standard library plus the modules an administrator allowed (" + ", ".join(allowed) + ")"
        return (
            "Runtime (Python 3):\n"
            "- The code runs at module top level, after the platform's own setup lines: `return` is not "
            "allowed, nor is a `from __future__` import, and nothing is called for you (call any function "
            "you define).\n"
            "- Each input the caller passes is a variable named as listed below. An input the caller omits "
            "is NOT defined: read an optional one with `__xtm_tool_inputs__.get(\"PARAMETER_NAME\")`, "
            "PARAMETER_NAME being the parameter's own name (that dict holds every input passed).\n"
            "- The tool's result is what the code prints: `print(...)` it, as `json.dumps(value, indent=2)` "
            "for structured data.\n"
            "- To fail, raise an exception with a clear message: it is reported to the agent.\n"
            f"- {libraries}, and these modules cannot be imported: " + ", ".join(sorted(blocked)) + ".\n"
            f"- {network}"
        )
    return (
        "Runtime (JavaScript, Node.js):\n"
        "- The code is the BODY of an async function: top-level `await` and `return` are allowed. Write "
        "statements, not a function nobody calls, and no `import` / `export`.\n"
        "- Each input the caller passes is a constant named as listed below. An input the caller omits is "
        "NOT declared: check an optional one with `typeof $name === \"undefined\"` before reading it.\n"
        "- The tool's result is what the code writes to standard output: `console.log(...)` it, as "
        "`JSON.stringify(value, null, 2)` for structured data. A returned value is NOT output.\n"
        "- To fail, `throw new Error(\"clear message\")`: the message is reported to the agent.\n"
        "- Node.js built-in modules only (`require(\"crypto\")`, ...): no npm package is installed, and "
        "the file system and child processes are unavailable.\n"
        f"- {network}"
    )


_OUTPUT_RULES = (
    "Rules:\n"
    "- Never write a secret (API key, token, password) in the code. Keep every `{{var_id.VARIABLE_ID}}` "
    "placeholder of the current code exactly as written, and never invent one - the tool refuses to run "
    "with an unknown placeholder. Where a secret is needed and none exists, leave an empty constant with "
    "a comment asking the user to insert a platform variable there (the editor's Variable button).\n"
    "- If the request needs something the sandbox does not offer, write the closest working version and "
    "say what is missing in a comment at the top.\n"
    "- Answer with the complete code only: no explanation around it and no markdown fence. Comments in "
    "the code are welcome where they help."
)


def build_messages(
    *,
    language: str,
    brief: str,
    tool_name: str,
    tool_description: str,
    parameters: Sequence[ToolParameter],
    current_code: str | None,
    sandbox: str,
    allow_list: bool,
    blocked: frozenset[str],
) -> list[ChatMessage]:
    """The request for one draft: the runtime contract, then the tool and the brief."""
    declared: dict[str, ToolParameter] = {}
    for param in parameters:
        if param.name != FILES_INPUT:
            declared.setdefault(param.name, param)
    bindings = input_bindings(language, [*declared, FILES_INPUT])
    network = network_note(language, sandbox, allow_list=allow_list, blocked=blocked)

    system = (
        "You write the code of a custom tool that AI agents call on an automation platform. The code runs "
        "in a sandbox with the inputs the agent passes; what it outputs is the tool's result.\n\n"
        + _runtime_rules(language, network, blocked)
        + "\n\n"
        + _OUTPUT_RULES
    )

    parts = [f"Language: {'Python' if language == 'python' else 'JavaScript'}"]
    if tool_name.strip():
        parts.append(f"Tool name: {tool_name.strip()}")
    if tool_description.strip():
        parts.append(f"Tool description: {tool_description.strip()}")
    parts.append(
        "Inputs:\n"
        + "\n".join(_parameter_line(declared.get(b.name), b, language) for b in bindings)
    )
    parts.append(f"Request:\n{brief.strip()}")
    if current_code and current_code.strip():
        parts.append(f"Current code (rework it to fulfil the request):\n{current_code}")
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content="\n\n".join(parts))]


async def _lint(language: str, code: str) -> dict:
    from app.services.custom_tool_executor import lint_tool_code

    try:
        return await lint_tool_code(language, code)
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}


async def generate_tool_code(
    llm: object,
    model: str | None,
    *,
    language: str,
    brief: str,
    tool_name: str = "",
    tool_description: str = "",
    parameters: Sequence[ToolParameter] = (),
    current_code: str | None = None,
) -> GeneratedCode:
    """Draft the code, syntax-check it, and hand a parser error back once.

    Raises :class:`CodeGenerationError` when the model fails or returns
    nothing; a cut or non-parsing answer is returned, flagged.
    """
    from app.core.config import settings
    from app.services.custom_tool_executor import _resolve_custom_tool_allowed_modules, get_effective_sandbox

    blocked = effective_blocked_modules(_resolve_custom_tool_allowed_modules())
    messages = build_messages(
        language=language,
        brief=brief,
        tool_name=tool_name,
        tool_description=tool_description,
        parameters=parameters,
        current_code=current_code,
        sandbox=await get_effective_sandbox(),
        allow_list=bool(settings.opensandbox_network_allow_list),
        blocked=blocked,
    )

    async def _ask(conversation: list[ChatMessage]) -> tuple[str, bool]:
        async def _call(max_tokens: int):
            return await llm.chat(conversation, model=model, temperature=0.2, max_tokens=max_tokens)  # type: ignore[attr-defined]

        response, cut = await chat_retrying_a_cut(llm, model, CODE_MAX_TOKENS, _call, label="Custom tool code")
        return unwrap_code(getattr(response, "content", None)), cut

    try:
        code, cut = await _ask(messages)
    except Exception as exc:
        logger.warning("Custom tool code generation failed: %s", format_exception_message(exc))
        raise CodeGenerationError("The assistant could not write the code right now. Try again.") from exc
    if not code:
        raise CodeGenerationError("The assistant returned no code. Try describing the tool differently.")

    verdict = await _lint(language, code)
    repaired = False
    if verdict.get("status") != "ok" and "line" in verdict:
        where = f" at line {verdict['line']}" if verdict.get("line") else ""
        retry = [
            *messages,
            ChatMessage(role="assistant", content=code),
            ChatMessage(
                role="user",
                content=(
                    f"The syntax check failed{where}: {verdict.get('message', '')}\n"
                    "Return the corrected complete code only."
                ),
            ),
        ]
        try:
            fixed, fixed_cut = await _ask(retry)
        except Exception as exc:
            logger.warning("Custom tool code repair failed: %s", format_exception_message(exc))
            fixed, fixed_cut = "", False
        if fixed:
            code, cut, repaired = fixed, fixed_cut, True
            verdict = await _lint(language, code)

    if cut:
        logger.warning("Custom tool code still cut at max_tokens - returned as generated")
    ok = verdict.get("status") == "ok"
    line = verdict.get("line")
    return GeneratedCode(
        code=code,
        truncated=cut,
        syntax_ok=ok,
        syntax_error=None if ok else str(verdict.get("message") or "The code does not parse."),
        syntax_error_line=line if not ok and isinstance(line, int) else None,
        repaired=repaired,
    )


__all__ = [
    "BRIEF_MAX_CHARS",
    "CODE_MAX_TOKENS",
    "CURRENT_CODE_MAX_CHARS",
    "FILES_INPUT",
    "LANGUAGES",
    "MAX_PARAMETERS",
    "PARAMETER_DESCRIPTION_MAX_CHARS",
    "TOOL_DESCRIPTION_MAX_CHARS",
    "CodeGenerationError",
    "GeneratedCode",
    "InputBinding",
    "ToolParameter",
    "build_messages",
    "generate_tool_code",
    "input_bindings",
    "network_note",
    "unwrap_code",
]
