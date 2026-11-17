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

"""The run's input as the ``$input`` session reference.

A transformer agent receives a document (a STIX bundle after a
``--- STIX BUNDLE ---`` line, say) and must hand parts of it to its tools. With
``$input`` it references them (``$input``, ``$input.objects[3].description``)
instead of copying them through its own output, so they reach the tool
byte-identical and cost no output tokens.

Path syntax (shared with ``$collected``, ``agent_loop._walk_session_path``):
``.key``; ``[0]``, a list index or the i-th value of an object; ``["any-key"]``
or ``['any-key']`` for a key holding a dash, dot, colon or space, a backslash
escaping a quote inside: ``$input.objects[0].extensions["extension-definition--UUID"].content``.

Opt-in per agent: ``config.output_mode = "patch"`` or ``config.input_reference = true``.
An agent that finalises on ``stix_bundle_validator`` (``config.finalize_on_builtin_tool``)
is opted in by default, patch mode being the documented way to finalise on that
tool; ``config.input_reference = false`` opts it out.

On a run that is not opted in, a ``$input`` placeholder in a tool argument is
refused with a message naming the opt-in (:func:`unexposed_input_reference_error`)
instead of reaching the tool or failing schema validation as a literal.

Out of the prompt: on a run that exposes ``$input``, a document over
:data:`INPUT_INLINE_MAX_CHARS` is cut out of the message it came in and
replaced by a compact reference block (:func:`detach_input_document`): its
size and shape and how to read it. The model reaches it through ``$input`` in
a tool argument, or whole in ``code_interpreter`` at :data:`INPUT_MOUNT_NAME`
under ``attachments/`` (:func:`input_document_bytes`). A playbook bundle of a
few million characters otherwise overflows the context window on the first
request, and is re-sent on every iteration when it fits.

The size protection does not depend on the opt-in: on a run that does not
expose ``$input`` (``input_reference: false`` included), a document too large
for the model window (:func:`oversize_input_chars`) is detached and mounted
all the same, its block naming the sandbox file only; ``$input`` stays
unresolved in tool arguments.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

INPUT_VARIABLE = "$input"

#: A document up to this many characters stays inline in the message: the
#: model reads a small input where it is, as it always did.
INPUT_INLINE_MAX_CHARS = 100_000
#: The name the detached document is mounted under in ``code_interpreter``
#: (``attachments/input.json``, listed in ``attachments/index.json``).
INPUT_MOUNT_NAME = "input.json"
#: Object types listed by name in the reference block; the rest are counted.
_REFERENCE_MAX_TYPES = 25
#: On a run that does not expose ``$input``, a document weighing more than
#: this share of the model window is still detached: left inline it cannot
#: leave room for the instructions, the tools and the answer.
OVERSIZE_INPUT_WINDOW_SHARE = 0.5

# ``--- STIX BUNDLE ---`` and the like: the document follows the last such line.
_DELIMITER_RE = re.compile(r"^[ \t]*-{3,}[^\n]*-{3,}[ \t]*$", re.MULTILINE)


# Built-in tools whose finalisation is documented in patch mode: an agent that
# finalises on one of them exposes ``$input`` unless it opts out.
_PATCH_FINALIZE_TOOLS = frozenset({"stix_bundle_validator"})

OPT_IN_HINT = 'agent config output_mode: "patch" or input_reference: true'


def input_reference_enabled(config: object) -> bool:
    """Whether an agent's *config* exposes its input as ``$input``."""
    if not isinstance(config, dict):
        return False
    explicit = config.get("input_reference")
    if explicit is False:
        return False
    if config.get("output_mode") == "patch" or explicit is True:
        return True
    finalize = config.get("finalize_on_builtin_tool")
    return isinstance(finalize, dict) and finalize.get("tool_name") in _PATCH_FINALIZE_TOOLS


def oversize_input_chars(context_window_tokens: int, chars_per_token: int) -> int:
    """The document size over which a run that does not expose ``$input`` still detaches it."""
    return max(INPUT_INLINE_MAX_CHARS, int(context_window_tokens * chars_per_token * OVERSIZE_INPUT_WINDOW_SHARE))


def carries_stix_bundle(text: object) -> bool:
    """Whether *text* (a run's input message) carries a STIX bundle document."""
    return isinstance(text, str) and _is_stix_bundle(parse_input_document(text))


def input_reference_for_run(config: object, input_text: object) -> bool:
    """Whether a run exposes its input as ``$input``.

    The agent's opt-in (:func:`input_reference_enabled`), or an input that
    carries a STIX bundle (an OpenCTI playbook sends the bundle with its
    instructions as the message) unless the agent opted out with
    ``input_reference: false``.
    """
    if input_reference_enabled(config):
        return True
    if isinstance(config, dict) and config.get("input_reference") is False:
        return False
    return carries_stix_bundle(input_text)


def normalize_input_placeholder(value: str) -> str:
    """*value* without the quotes or spaces a model wraps a placeholder in.

    ``'"$input"'``, ``"'$input.objects[0]'"`` and ``' $input '`` read as the
    placeholder they quote; any other text is returned unchanged.
    """
    stripped = value.strip()
    # An escaped quote pair (``\\"$input\\"``): a model that JSON-encodes the
    # quoted placeholder once more.
    if len(stripped) >= 4 and stripped.startswith('\\"') and stripped.endswith('\\"'):
        stripped = stripped[2:-2].strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'`":
        stripped = stripped[1:-1].strip()
    if stripped == INPUT_VARIABLE or stripped.startswith((INPUT_VARIABLE + ".", INPUT_VARIABLE + "[")):
        return stripped
    return value


def _declared_string(parameter_schema: object) -> bool:
    declared = parameter_schema.get("type") if isinstance(parameter_schema, dict) else None
    return declared == "string" or (isinstance(declared, list) and "string" in declared)


def unexposed_input_reference_error(key: str, value: object, parameter_schema: object) -> str | None:
    """The refusal for a ``$input`` placeholder on a run that does not expose it.

    ``None`` when *value* is not a placeholder, and for any value given to a
    parameter declared as a string: a search text, a query or a span filter
    that reads ``$input`` is real text, passed through untouched.
    """
    if not isinstance(value, str):
        return None
    value = normalize_input_placeholder(value)
    if value != INPUT_VARIABLE and not value.startswith((INPUT_VARIABLE + ".", INPUT_VARIABLE + "[")):
        return None
    if _declared_string(parameter_schema):
        return None
    return (
        f"Argument '{key}' is '{value}', but $input is not exposed to this agent, so "
        f"the placeholder cannot be resolved. The tool was NOT executed. Pass the "
        f"object itself in '{key}', or expose the input with {OPT_IN_HINT}."
    )


# A fenced block opening line (```json, ```) the document may sit in.
_FENCE_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_-]*[ \t]*$", re.MULTILINE)
# Where a STIX bundle object starts, wherever it sits in the text.
_BUNDLE_START_RE = re.compile(r'\{\s*"(?:type|id|spec_version|objects)"\s*:')


def _decode_span(text: str, start: int) -> tuple[Any, int, int] | None:
    """The JSON object or array starting at *start* (leading blanks skipped), with its span."""
    begin = start
    while begin < len(text) and text[begin].isspace():
        begin += 1
    if begin >= len(text) or text[begin] not in "{[":
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(text, begin)
    except json.JSONDecodeError:
        return None
    return (value, begin, end) if isinstance(value, (dict, list)) else None


def _is_stix_bundle(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "bundle" and isinstance(value.get("objects"), list)


def parse_input_document(text: str) -> Any | None:
    """The JSON document *text* carries.

    Read, in order: what follows each delimiter line (``--- STIX BUNDLE ---``),
    the last one first, so a block the platform appends after the document (a
    brief with its own heading) never hides it, and instructions that begin
    with a JSON example never stand for it; the whole text; what follows each
    fence line; then the first STIX bundle object found anywhere in the text
    (instructions before it, text after it). A JSON value found that is not a
    STIX bundle gives way to a STIX bundle found after it (an example ahead of
    the real input). ``None`` when no JSON object or array is found:
    ``$input`` is then left undefined and a reference to it is reported,
    never passed through.
    """
    found = locate_input_document(text)
    return None if found is None else found[0]


def locate_input_document(text: str) -> tuple[Any, int, int] | None:
    """The document :func:`parse_input_document` reads, with its ``[start, end)`` span in *text*."""
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = _first_candidate(text)
    if candidate is not None and _is_stix_bundle(candidate[0]):
        return candidate
    bundle = _first_bundle(text, 0 if candidate is None else candidate[2])
    return bundle if bundle is not None else candidate


def _first_candidate(text: str) -> tuple[Any, int, int] | None:
    """The first JSON value after a delimiter line, at the start, or after a fence line."""
    for match in reversed(list(_DELIMITER_RE.finditer(text))):
        found = _decode_span(text, match.end())
        if found is not None:
            return found
    whole = _decode_span(text, 0)
    if whole is not None:
        return whole
    for match in reversed(list(_FENCE_RE.finditer(text))):
        found = _decode_span(text, match.end())
        if found is not None:
            return found
    return None


def _first_bundle(text: str, start: int) -> tuple[Any, int, int] | None:
    """The first STIX bundle object found in *text* at or after *start*."""
    for match in _BUNDLE_START_RE.finditer(text, start):
        found = _decode_span(text, match.start())
        if found is not None and _is_stix_bundle(found[0]):
            return found
    return None


def input_document_from_messages(messages: Sequence[Any]) -> Any | None:
    """The JSON document of the newest user message that carries one.

    The newest first: on the chat path the history read back for the turn
    can hold an earlier (or a cut) copy of a message, and the run's input is
    the current turn's message, the last one.
    """
    found = _newest_input_message(messages)
    return None if found is None else found[1]


def _newest_input_message(messages: Sequence[Any]) -> tuple[int, Any, int, int] | None:
    """``(index, document, start, end)`` of the newest user message carrying a document."""
    items = list(messages)
    for index in range(len(items) - 1, -1, -1):
        message = items[index]
        if getattr(message, "role", None) != "user":
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str):
            found = locate_input_document(content)
            if found is not None:
                return index, *found
    return None


def input_reference_block(document: Any, size_chars: int, *, exposed: bool = True) -> str:
    """The block that stands for a detached document in its message.

    Its size and shape - the object count and the counts by ``type`` for a
    document holding an ``objects`` list (a STIX bundle), the top-level keys
    otherwise - and the ways to read it: ``$input`` when the run exposes it
    (*exposed*), the sandbox file always. Generic: any JSON document.
    """
    read_whole = f"read it whole in code_interpreter: json.load(open('attachments/{INPUT_MOUNT_NAME}'))."
    lines = [
        f"[Input document detached from this message: {size_chars:,} characters, kept out of the prompt.",
        (
            f'Reference it as {INPUT_VARIABLE} in a tool argument (whole: "{INPUT_VARIABLE}"; '
            f"a part: {INPUT_VARIABLE}.objects[0].name), or {read_whole}"
            if exposed
            else f"It is too large for the model window: {read_whole[0].upper()}{read_whole[1:]}"
        ),
    ]
    objects = document.get("objects") if isinstance(document, dict) else None
    if isinstance(document, dict):
        header = {
            key: value
            for key, value in document.items()
            if key != "objects" and isinstance(value, (str, int, float, bool)) and len(str(value)) <= 200
        }
        if header:
            # key=value, never JSON: the block must not read as a document itself.
            lines.append("Top-level fields: " + ", ".join(f"{key}={value}" for key, value in header.items()))
    if isinstance(objects, list):
        counts = Counter(
            str(item.get("type")) if isinstance(item, dict) and item.get("type") is not None else "(no type)"
            for item in objects
        )
        shown = counts.most_common(_REFERENCE_MAX_TYPES)
        by_type = ", ".join(f"{name} {count}" for name, count in shown)
        rest = len(counts) - len(shown)
        if rest > 0:
            by_type += f", +{rest} more type(s)"
        lines.append(f"objects: {len(objects)} ({by_type})" if objects else "objects: 0")
    elif isinstance(document, dict):
        keys = list(document)
        lines.append("Top-level keys: " + ", ".join(str(k) for k in keys[:_REFERENCE_MAX_TYPES]))
    elif isinstance(document, list):
        lines.append(f"A JSON array of {len(document)} item(s).")
    return "\n".join(lines) + "]"


def detach_input_document(
    messages: list[Any], *, max_inline_chars: int = INPUT_INLINE_MAX_CHARS, exposed: bool = True
) -> tuple[Any | None, bool]:
    """The run's input document, cut out of its message when it is large.

    Finds the document :func:`input_document_from_messages` reads and, when
    its text is over *max_inline_chars*, replaces that span of the message by
    :func:`input_reference_block`: the message is rebuilt (never mutated in
    place, the caller's history object is shared) at the same position in
    *messages*, everything around the document - the instructions, the
    delimiter line, a brief after it - kept byte for byte. A document at or
    under the limit stays where it is. *exposed* says whether the block
    offers ``$input`` (:func:`input_reference_block`). An earlier user
    message holding the very same text (a history that still carries the
    current turn) gets the same block: no copy of the document is left
    inline. Returns ``(document, detached)``, ``(None, False)`` when no
    message carries one.
    """
    found = _newest_input_message(messages)
    if found is None:
        return None, False
    index, document, start, end = found
    if end - start <= max_inline_chars:
        return document, False
    message = messages[index]
    content = message.content
    block = input_reference_block(document, end - start, exposed=exposed)
    new_content = content[:start] + block + content[end:]
    for position, other in enumerate(messages):
        if position == index or (getattr(other, "role", None) == "user" and getattr(other, "content", None) == content):
            _set_content(messages, position, new_content)
    return document, True


def _set_content(messages: list[Any], position: int, content: str) -> None:
    """Give the message at *position* this *content*, a new object for a dataclass."""
    message = messages[position]
    if dataclasses.is_dataclass(message) and not isinstance(message, type):
        messages[position] = dataclasses.replace(message, content=content)
    else:
        message.content = content


#: The most characters of a task text a delegated run's system prompt
#: copies (the handover context line): the whole task is the run's message.
PROMPT_TASK_TEXT_MAX_CHARS = 4_000


def prompt_safe_task_text(text: object, *, max_chars: int | None = PROMPT_TASK_TEXT_MAX_CHARS) -> str:
    """*text* as a prompt line may copy it: never a detached document's body.

    A JSON document over :data:`INPUT_INLINE_MAX_CHARS` is replaced by the
    :func:`input_reference_block` the run's message carries, then the text
    is cut at *max_chars* (``None``: never cut) with a pointer to the message
    and the staged file.
    The one function every copy of a task text into a prompt goes through
    (``handover.format_handover_context_prompt``): a delegated run otherwise
    gets its input twice, once in the message and once in the system prompt.
    """
    if not isinstance(text, str) or not text:
        return ""
    found = locate_input_document(text)
    if found is not None:
        document, start, end = found
        if end - start > INPUT_INLINE_MAX_CHARS:
            text = text[:start] + input_reference_block(document, end - start) + text[end:]
    if max_chars is None or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f" [... {len(text) - max_chars:,} more characters cut: the whole task is the message you received;"
        f" a detached document is at attachments/{INPUT_MOUNT_NAME}.]"
    )


def input_document_bytes(document: Any) -> bytes:
    """*document* as the UTF-8 JSON mounted at ``attachments/input.json``."""
    return json.dumps(document, ensure_ascii=False).encode("utf-8")
