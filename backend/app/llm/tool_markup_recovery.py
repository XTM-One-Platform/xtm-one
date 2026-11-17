# SPDX-License-Identifier: Apache-2.0

"""Recover an Anthropic tool call a model emitted as text, at the adapter.

Claude models sometimes serialise a tool call's own markup — the
``<function_calls>`` / ``<invoke name=...>`` / ``<parameter name=...>`` wrapper,
with or without the ``antml:`` prefix — into their TEXT output instead of
returning a structured ``tool_use`` block. The call then never runs and the turn
stalls on a "final" answer that is really a dropped call.

This is a vendor concern (the markup is Anthropic-specific), so it is handled in
the provider adapter, next to the other Anthropic response parsing, rather than
in the generic agent loop. Only the shape of a real leak is acted on: the model
ENDS its answer with the markup, outside any code fence or quoted line, in a
request that offered tools. Markup anywhere else is the model quoting or
explaining it (a phishing payload under analysis, a retrieved document, a
question about the format) and stays text, never executed.

A trailing leak becomes real ``ToolCall`` objects only when that is safe:
every name is a tool the request declared, at most ``_MAX_RECOVERED_CALLS``
calls, no two identical. Any other trailing leak (malformed, truncated, an
undeclared name, a looped or oversized batch) is surfaced to the loop through
the generic ``ChatResponse.leaked_tool_markup_unrecovered`` flag, so the loop can
re-prompt without knowing any vendor markup.

Shared by every Claude-serving provider (native Anthropic, and Bedrock for its
Claude models).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.tools.base import ToolCall, ToolDefinition

# Detection — high precision. The ``function_calls`` wrapper tag and the
# ``antml:`` prefix do not occur in prose or real XML, and ``<invoke name=...>``
# is the Anthropic call shape, unlike a legitimate BPEL ``<invoke
# partnerLink=...>`` in an answer, which carries no ``name`` attribute.
_LEAKED_TOOL_CALL_RE = re.compile(
    r"</?\s*antml:(?:function_calls|invoke)\b"  # antml:-prefixed: definitively Anthropic
    r"|</?\s*function_calls\b"  # the wrapper tag, absent from prose and real XML
    r"|<\s*invoke\s+name\s*=",  # <invoke name="..."> - Anthropic, not BPEL partnerLink
    re.IGNORECASE,
)

# One ``<invoke name="tool">...</invoke>`` block (optionally ``antml:``-prefixed).
_INVOKE_RE = re.compile(
    r"<\s*(?:antml:)?invoke\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</\s*(?:antml:)?invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)

# One ``<parameter name="p">value</parameter>`` inside an invoke block.
_PARAMETER_RE = re.compile(
    r"<\s*(?:antml:)?parameter\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</\s*(?:antml:)?parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

# The whole ``<function_calls>...</function_calls>`` wrapper, and a stray
# closing wrapper tag, to strip them out.
_FUNCTION_CALLS_BLOCK_RE = re.compile(
    r"<\s*(?:antml:)?function_calls\s*>.*?</\s*(?:antml:)?function_calls\s*>",
    re.IGNORECASE | re.DOTALL,
)
_FUNCTION_CALLS_CLOSE_RE = re.compile(r"</\s*(?:antml:)?function_calls\s*>", re.IGNORECASE)

# A fenced code block (closed, or running to the end of the text) and a quoted
# line: markup inside either is quoted, never a leak.
_FENCE_RE = re.compile(
    r"^[ \t]*(```|~~~)[^\n]*\n.*?(?:^[ \t]*\1[^\n]*$|\Z)", re.MULTILINE | re.DOTALL
)
_QUOTED_LINE_RE = re.compile(r"^[ \t]*>[^\n]*$", re.MULTILINE)

#: The most calls one leak is recovered into: a model looping on an
#: ``<invoke>`` block must not turn into as many executions.
_MAX_RECOVERED_CALLS = 5

# Characters a provider accepts in a tool-use id (Anthropic: ``^[a-zA-Z0-9_-]+$``).
_UNSAFE_ID_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]+")


def looks_like_leaked_tool_call(text: str) -> bool:
    """True when *text* carries tool-call markup, wherever it is."""
    if not text:
        return False
    return bool(_LEAKED_TOOL_CALL_RE.search(text))


@dataclass
class LeakedToolCallRecovery:
    """Outcome of inspecting a tool-free answer for a trailing leaked tool call.

    * ``tool_calls`` non-empty — the markup parsed into calls to declared tools;
      ``content`` is the text before the markup. The caller uses these as if the
      model had returned structured ``tool_use`` blocks.
    * ``unrecovered`` True — the answer ends with markup that is not safe to run
      (malformed, truncated, an undeclared tool, a looped or oversized batch);
      ``content`` is unchanged and the caller flags it so the loop re-prompts.
    """

    tool_calls: list[ToolCall]
    content: str
    unrecovered: bool


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCE_RE.finditer(text)]
    spans += [m.span() for m in _QUOTED_LINE_RE.finditer(text)]
    return spans


def _trailing_markup_start(text: str) -> int | None:
    """Where the answer's trailing leaked markup starts, or ``None``.

    The first markup outside a code fence or a quoted line must start a tail
    that is nothing but markup: complete ``function_calls`` / ``invoke``
    blocks, possibly followed by one left unterminated (a truncated leak).
    Prose after the markup means the model was quoting or explaining it.
    """
    spans = _quoted_spans(text)
    for match in _LEAKED_TOOL_CALL_RE.finditer(text):
        start = match.start()
        if any(lo <= start < hi for lo, hi in spans):
            continue
        tail = _FUNCTION_CALLS_BLOCK_RE.sub("", text[start:])
        tail = _FUNCTION_CALLS_CLOSE_RE.sub("", _INVOKE_RE.sub("", tail))
        rest = tail.strip()
        if not rest or _LEAKED_TOOL_CALL_RE.match(rest):
            return start
        return None
    return None


def _coerce_argument(value: str, schema: dict[str, Any] | None) -> Any:
    """A parameter's text as the type its tool schema declares.

    The native format sends a non-string parameter as JSON (``5``, ``true``,
    ``[...]``), so a parameter the schema types as anything but a string is
    decoded, falling back to the raw text when it is not JSON.
    """
    if not schema or schema.get("type") == "string":
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _recovered_call_id(name: str) -> str:
    # Unique across the conversation: a model that leaks once tends to leak
    # again, and a repeated tool-use id makes the next request invalid.
    safe_name = _UNSAFE_ID_CHARS_RE.sub("_", name)[:40] or "tool"
    return f"recovered_{safe_name}_{uuid.uuid4().hex[:12]}"


def recover_leaked_tool_calls(
    content: str | None,
    existing_tool_calls: list[ToolCall] | None,
    tools: list[ToolDefinition] | None,
) -> LeakedToolCallRecovery | None:
    """Parse a trailing leaked tool call out of a tool-free answer.

    Returns ``None`` when there is nothing to do: the request offered no tools,
    the model already returned a structured tool call, or the answer does not
    END with unquoted tool-call markup. Otherwise returns a
    :class:`LeakedToolCallRecovery` describing what was recovered.
    """
    if existing_tool_calls or not tools:
        return None
    text = content or ""
    if not looks_like_leaked_tool_call(text):
        return None
    start = _trailing_markup_start(text)
    if start is None:
        return None

    unrecovered = LeakedToolCallRecovery(tool_calls=[], content=text, unrecovered=True)
    declared = {tool.name: tool for tool in tools}
    blocks = _INVOKE_RE.findall(text[start:])
    if not blocks or len(blocks) > _MAX_RECOVERED_CALLS:
        return unrecovered
    if len({(name, body.strip()) for name, body in blocks}) < len(blocks):
        # The same call written twice is a loop, not a batch.
        return unrecovered
    if any(name not in declared for name, _body in blocks):
        return unrecovered

    tool_calls: list[ToolCall] = []
    for name, body in blocks:
        properties = (declared[name].input_schema or {}).get("properties") or {}
        arguments = {
            param: _coerce_argument(value.strip(), properties.get(param))
            for param, value in _PARAMETER_RE.findall(body)
        }
        tool_calls.append(
            ToolCall(id=_recovered_call_id(name), name=name, arguments=arguments)
        )

    return LeakedToolCallRecovery(
        tool_calls=tool_calls, content=text[:start].rstrip(), unrecovered=False
    )
