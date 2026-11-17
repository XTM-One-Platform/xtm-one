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

"""Output format prompt injection for agent chat.

Defines the format instructions injected into system prompts based on
the agent's configured ``output_format`` and, when the message arrives
via an external channel (Slack, Teams, etc.), the channel-specific
formatting rules that override the agent setting.

When the agent has ``output_format = "json"`` and an ``output_schema``
(JSON Schema dict), the schema is converted to readable instructions
via the SAP (Schema-Aligned Parsing) module.
"""

from __future__ import annotations

from typing import Any

# ── Agent output format instructions ────────────────────────────────
# These are used when the agent is chatting via web or when no channel
# context is present.
OUTPUT_FORMAT_PROMPTS: dict[str, str] = {
    "markdown": (
        "Format your responses using standard Markdown (headings, bold, italic, lists, code blocks, links)."
    ),
    "html": (
        "Format your responses using HTML markup. "
        "Use semantic elements (h1–h6, p, ul/ol, code, a, strong, em) "
        "and produce clean, valid HTML fragments (no <html>/<body> wrappers)."
    ),
    "json": (
        "Format your responses as valid JSON. "
        'Return a JSON object with a top-level "response" key containing '
        "your answer. When the answer includes structured data, organise it "
        "into appropriate keys. Always output valid, parseable JSON."
    ),
    "plain_text": (
        "Format your responses as plain text without any formatting markup. "
        "Do not use Markdown, HTML, or any other formatting syntax. "
        "Use line breaks and spacing for readability."
    ),
}

# ── Channel-specific format overrides ───────────────────────────────
# When the message arrives from an external channel, the format is
# forced to what the channel natively supports, regardless of the
# agent's configured output_format.
CHANNEL_FORMAT_OVERRIDES: dict[str, str] = {
    "slack": (
        "You are responding in Slack. Use Slack mrkdwn formatting:\n"
        "- Bold: *bold* (NOT **bold**)\n"
        "- Italic: _italic_ (NOT *italic*)\n"
        "- Strikethrough: ~text~\n"
        "- Code: `code` for inline, ```code``` for blocks\n"
        "- Links: <url|display text>\n"
        "- Lists: use simple bullet points with • or -\n"
        "- Blockquotes: > text\n"
        "- No headings (# is not supported in Slack)"
    ),
    "teams": (
        "You are responding in Microsoft Teams. Use standard Markdown:\n"
        "- Bold: **bold**, Italic: *italic*\n"
        "- Headings: # ## ###\n"
        "- Code: `code` for inline, ```code``` for blocks\n"
        "- Lists: numbered (1. 2. 3.) and bullet (- or *)\n"
        "- Section labels are bold lines on their own, never list items. "
        "No nested lists: Teams flattens them.\n"
        "- Links: [text](url)\n"
        "- Tables are supported with | pipes"
    ),
    "whatsapp": (
        "You are responding in WhatsApp. Use WhatsApp formatting:\n"
        "- Bold: *bold*\n"
        "- Italic: _italic_\n"
        "- Strikethrough: ~text~\n"
        "- Monospace: ```text```\n"
        "- No headings, no links, no complex formatting\n"
        "- Keep messages concise — WhatsApp is a mobile-first platform\n"
        "- Use line breaks for readability"
    ),
    "telegram": (
        "You are responding in Telegram. Use Telegram MarkdownV2 formatting:\n"
        "- Bold: *bold*\n"
        "- Italic: _italic_\n"
        "- Underline: __underline__\n"
        "- Strikethrough: ~text~\n"
        "- Code: `code` for inline, ```code``` for blocks\n"
        "- Links: [text](url)\n"
        "- Keep messages concise"
    ),
    "discord": (
        "You are responding in Discord. Use Discord Markdown:\n"
        "- Bold: **bold**, Italic: *italic*, Underline: __text__\n"
        "- Strikethrough: ~~text~~\n"
        "- Code: `code` for inline, ```language\\ncode``` for blocks\n"
        "- Headings: # ## ###\n"
        "- Spoilers: ||text||\n"
        "- Blockquotes: > text or >>> multi-line\n"
        "- Keep messages under 2000 characters per message"
    ),
    "google_chat": (
        "You are responding in Google Chat. Use simple formatting:\n"
        "- Bold: *bold*\n"
        "- Italic: _italic_\n"
        "- Strikethrough: ~text~\n"
        "- Code: `code` for inline, ```code``` for blocks\n"
        "- Links: [text](url) or raw URLs\n"
        "- Keep formatting simple — complex Markdown is not fully supported"
    ),
}

# ── The markup a stored reply is written in ─────────────────────────
#: Message metadata naming the markup of a reply written under a channel
#: override the web does not render as is. Absent: the standard
#: Markdown the web renders, whatever channel the reply went to - the
#: General Assistant's replies, the router's own notices and a wake's
#: reply are Markdown on Slack too, converted only on their way out.
CONTENT_FORMAT_META_KEY = "content_format"
#: Slack's mrkdwn (``CHANNEL_FORMAT_OVERRIDES["slack"]``).
SLACK_MRKDWN = "slack_mrkdwn"

_OVERRIDE_CONTENT_FORMATS: dict[str, str] = {"slack": SLACK_MRKDWN}


def effective_output_contract(
    agent_format: str | None,
    agent_schema: dict[str, Any] | None,
    session_schema: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    """The format and schema one run of an agent answers in.

    A session schema (a flow's wired ``capability.structured_output``)
    REPLACES the agent's own and makes the run JSON; ``None`` leaves the agent
    as it is configured. One rule for the run (``agent_chat``), the flow node
    that publishes the answer's ``fields``, and the builder's declared shapes
    (``flow_output_schemas``).
    """
    if session_schema is not None:
        return "json", session_schema
    return agent_format or "markdown", agent_schema


#: Largest per-call ``output_schema`` a caller may hand ``consult_agent`` or
#: ``spawn_background_task``, serialized: the bound of a stored agent schema.
CALL_OUTPUT_SCHEMA_MAX_BYTES = 50_000

#: The ``output_schema`` argument of ``consult_agent`` and
#: ``spawn_background_task``: one property, one line, every handler alike.
CALL_OUTPUT_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional JSON Schema the answer must follow: the agent then answers one JSON "
        "object valid against it (50 KB max). The agent must have "
        "accept_caller_output_format on"
    ),
}

#: Task metadata key carrying a background task's per-call schema to its worker.
CALL_OUTPUT_SCHEMA_KEY = "call_output_schema"


def parse_call_output_schema(value: object) -> tuple[dict[str, Any] | None, str | None]:
    """Read a caller's per-call ``output_schema``: ``(schema, None)`` or ``(None, error)``.

    ``None`` or an empty value is no schema (the callee answers as configured).
    A JSON text is decoded (some models send an object argument as a string).
    Anything else must be a JSON Schema object under
    :data:`CALL_OUTPUT_SCHEMA_MAX_BYTES`, checked against Draft 7 as a stored
    agent schema is. The error is the tool result itself (an ``Error:`` text
    naming the problem): the call is refused before anything runs.
    """
    import json

    if value is None or value == "" or value == {}:
        return None, None
    problem: str | None = None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            problem = f"it is not valid JSON ({exc})"
    if problem is None and not isinstance(value, dict):
        problem = f"it must be a JSON Schema object, not {type(value).__name__}"
    if problem is None:
        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError) as exc:
            size, problem = 0, f"it cannot be serialized ({exc})"
        if problem is None and size > CALL_OUTPUT_SCHEMA_MAX_BYTES:
            problem = f"it is {size:,} bytes, over the {CALL_OUTPUT_SCHEMA_MAX_BYTES:,}-byte limit"
    if problem is None:
        from jsonschema import Draft7Validator, SchemaError  # type: ignore[import-untyped]

        try:
            Draft7Validator.check_schema(value)
        except SchemaError as exc:
            problem = f"it is not a valid JSON Schema ({exc.message})"
    if problem is None and isinstance(value, dict):
        return value, None
    return None, f"Error: output_schema refused, nothing ran: {problem}. Fix it and call again."


def caller_output_format_refusal(target: object, call_schema: dict[str, Any] | None) -> str | None:
    """The refusal of a per-call ``output_schema`` the target agent does not accept.

    ``None`` when no schema was passed or the target has
    ``accept_caller_output_format`` on; else the tool result itself, returned
    before the target runs. Off is the default (a missing attribute too): an
    agent answers in its configured format unless its manager opted in.
    """
    if call_schema is None or getattr(target, "accept_caller_output_format", False) is True:
        return None
    name = getattr(target, "name", None) or "this agent"
    return (
        f"Error: Agent {name} answers only in its configured format "
        "(accept_caller_output_format is off), nothing ran. Call it without "
        "output_schema or ask its owner to enable the setting."
    )


#: Handover types whose target answers a live chat user in the same
#: conversation (the web chat renders Markdown, never raw HTML).
_LIVE_CHAT_HANDOVER_TYPES = frozenset({"transfer", "handover"})


def answers_live_chat(handover_context: dict | None) -> bool:
    """Whether a run's answer goes to a person in a chat rather than to an agent.

    A run handed no context (a direct chat), a transfer or a handover answers
    the person in the conversation; a consult, a background task or an A2A
    partner's task answers the agent that asked.
    """
    if not handover_context:
        return True
    return handover_context.get("type", "handover") in _LIVE_CHAT_HANDOVER_TYPES


def chat_handover_output_format(agent_format: str, handover_context: dict | None) -> str:
    """The format a run answers in once a chat transfer made it the active agent.

    An ``html`` agent (written for callers that read HTML, such as intent
    calls) answers in Markdown when a transfer or handover hands it a live
    chat: its answer is the chat message, and the chat renders Markdown,
    so HTML would reach the user as raw tags. Every other format, and a
    run that was not handed a chat (a consult, a background task, a direct
    call), keeps the agent's own.
    """
    if agent_format == "html" and handover_context and answers_live_chat(handover_context):
        return "markdown"
    return agent_format


def override_content_format(channel_platform: str | None) -> str | None:
    """The markup of a reply an agent wrote under *channel_platform*'s override.

    ``None`` when that reply is standard Markdown (or the channel is never
    shown on the web). Ask it with the ``platform`` of the context the run
    was given: only a run that got the override writes in it.
    """
    return _OVERRIDE_CONTENT_FORMATS.get(str(channel_platform)) if channel_platform else None


def read_content_format(meta: object) -> str | None:
    """A stored message's :data:`CONTENT_FORMAT_META_KEY`, if it is a known one."""
    if not isinstance(meta, dict):
        return None
    value = meta.get(CONTENT_FORMAT_META_KEY)
    return value if value in _OVERRIDE_CONTENT_FORMATS.values() else None


def build_output_format_prompt(
    agent_format: str,
    channel_platform: str | None = None,
    output_schema: dict[str, Any] | None = None,
) -> str:
    """Build the output format system prompt section.

    When *channel_platform* is provided and matches a known channel,
    the channel-specific override takes precedence.  Otherwise the
    agent's configured *agent_format* is used.

    When *output_schema* is provided and *agent_format* is ``"json"``,
    the JSON Schema is converted into detailed output instructions using
    Schema-Aligned Parsing (SAP) so the LLM knows exactly what to return.

    Returns the prompt section string (may be empty for unknown formats).
    """
    platform = str(channel_platform) if channel_platform else None

    # JSON with schema → always include SAP prompt (even on external channels)
    schema_section = ""
    if agent_format == "json" and output_schema:
        from app.services.structured_output import build_schema_prompt

        schema_section = build_schema_prompt(output_schema)

    # Channel override for formatting style (Slack mrkdwn, Teams Markdown, etc.)
    if platform and platform in CHANNEL_FORMAT_OVERRIDES:
        if schema_section:
            # Schema instructions take priority; append channel formatting hints
            return (
                "\n── Output Format ──\n"
                + schema_section
                + "\n\n"
                + CHANNEL_FORMAT_OVERRIDES[platform]
            )
        return "\n── Output Format ──\n" + CHANNEL_FORMAT_OVERRIDES[platform]

    # JSON with schema (no channel override)
    if schema_section:
        return "\n── Output Format ──\n" + schema_section

    # Fall back to agent's configured format
    fmt_prompt = OUTPUT_FORMAT_PROMPTS.get(agent_format)
    if fmt_prompt:
        return "\n── Output Format ──\n" + fmt_prompt

    return ""
