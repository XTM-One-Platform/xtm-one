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

"""Markdown renderings of the chat commands' results (#6590).

What a surface without a rich rendering shows: the chat embedded in a module
before it renders the structured result, and the channels. Every text that
reaches a table goes through :func:`markdown_text`, ours included: a tool or
an MCP server name is chosen by a third party, and one escaping rule for every
cell is what keeps a name from closing the table, opening a link or an image,
or starting a new line.
"""

from __future__ import annotations

import re
import unicodedata

from app.services.chat_commands import ChatCommand, usage
from app.services.chat_commands.context_report import ContextCategory, ContextReport

#: A cell's text is cut to this many characters (the ellipsis included).
_MAX_CELL_CHARS = 80

#: Every ASCII punctuation character, which CommonMark lets a backslash escape:
#: escaping them all makes the text literal whatever the renderer's extensions.
_PUNCTUATION_RE = re.compile(r"([!-/:-@\[-`{-~])")

CATEGORY_LABELS: dict[str, str] = {
    "system_prompt": "System prompt",
    "memory": "Memory",
    "knowledge": "Workspace knowledge",
    "skills": "Skills",
    "builtin_tools": "Built-in tools",
    "mcp_tools": "MCP tools",
    "integration_tools": "Integration tools",
    "custom_tools": "Custom tools",
    "connected_tools": "MCP, integration and custom tools",
    "other_tools": "Other tools",
    "conversation_summary": "Conversation summary",
    "tool_results": "Tool results",
    "messages": "Messages",
    "unattributed": "Unattributed",
}

#: The labelled sections of a system prompt (``agent_chat._prompt_sections``).
SECTION_LABELS: dict[str, str] = {
    "persona": "Persona",
    "guardrails": "Guardrails",
    "attached_prompts": "Attached prompts",
    "autonomous_eval": "Evaluation instructions",
    "coordination_tools": "Coordination instructions",
    "platform": "Platform instructions",
    "user_memory": "Your memory",
    "agent_memory": "Agent memory",
    "workspace_instructions": "Workspace instructions",
    "workspace_sources": "Workspace sources",
    "skills": "Loaded skills",
    "skills_catalog": "Skills catalog",
}

#: Categories whose items are system-prompt sections, named by label.
_SECTION_CATEGORIES = frozenset({"system_prompt", "memory", "knowledge", "skills"})

_LEVEL_LABELS: dict[str, str] = {
    "normal": "normal",
    "approaching": "approaching: older turns will soon be summarized",
    "overflowed": "overflowed: older content was cut to fit",
}


def markdown_text(value: str, *, limit: int = _MAX_CELL_CHARS) -> str:
    """*value* as literal Markdown text on one line, at most *limit* characters.

    Control and separator characters (newlines, tabs, U+2028...) become
    spaces, runs of spaces one, and every ASCII punctuation character is
    backslash-escaped: no delimiter of a table, a link, an image, an emphasis,
    a code span, an HTML tag or an entity survives.
    """
    cleaned = "".join(
        " " if unicodedata.category(ch)[0] in "CZ" else ch for ch in value
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return _PUNCTUATION_RE.sub(r"\\\1", cleaned)


def _tokens(count: int) -> str:
    """``1234`` → ``1.2k``, ``1_250_000`` → ``1.25M``."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _share(count: int, window: int | None) -> str:
    if not window:
        return ""
    return f"{count / window * 100:.1f}%"


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"


def _item_name(category: ContextCategory, name: str) -> str:
    if category.key in _SECTION_CATEGORIES:
        return SECTION_LABELS.get(name, name)
    return name


_STATE_NOTES: dict[str, str] = {
    "measured": "Used is the prompt the provider counted on the last reply; the categories are estimates scaled to it.",
    "estimated": "Estimated from the last reply's request: the provider reported no token count for it.",
    "previous_conversation": (
        "No reply in this conversation yet: the system prompt and tools of your "
        "latest conversation with this agent, plus the messages written here."
    ),
}


def render_context_report(report: ContextReport) -> str:
    lines = ["## Context usage", ""]
    if report.state == "none":
        lines.append(
            markdown_text(
                "No context reading yet: send a message, and run the command again once the agent has answered.",
                limit=200,
            )
        )
        return "\n".join(lines)

    header = [f"Window: {_tokens(report.window or 0)} tokens"]
    if report.model:
        header.insert(0, f"Model: {report.model}")
    lines.append(markdown_text(" · ".join(header), limit=200))
    lines.append("")
    lines.append(f"_{markdown_text(_STATE_NOTES[report.state], limit=300)}_")
    if report.agent_changed:
        lines.append("")
        lines.append(
            f"_{markdown_text('The agent was edited since: its prompt or tools may differ on the next reply.', limit=200)}_"
        )
    lines.append("")

    used = report.used_tokens or 0
    lines += [_row("", "Tokens", "Window"), _row("---", "---:", "---:")]
    lines.append(_row("Used", _tokens(used), _share(used, report.window)))
    if report.cached_tokens:
        lines.append(_row("Cached prefix", _tokens(report.cached_tokens), _share(report.cached_tokens, report.window)))
    if report.new_message_tokens:
        lines.append(
            _row(
                "Last answer and messages since",
                _tokens(report.new_message_tokens),
                _share(report.new_message_tokens, report.window),
            )
        )

    if report.compaction is not None:
        compaction = report.compaction
        lines += ["", "**Compaction**", "", _row("", "Tokens"), _row("---", "---:")]
        lines.append(_row("Conversation now", _tokens(compaction.gate_tokens)))
        lines.append(_row("Warns at", _tokens(compaction.approaching_tokens)))
        lines.append(_row("Summarizes at", _tokens(compaction.trigger_tokens)))
        lines.append(_row("Summarizes down to", _tokens(compaction.target_tokens)))
        lines.append(_row("Free before summarizing", _tokens(compaction.free_tokens)))
        lines.append("")
        level = _LEVEL_LABELS.get(compaction.level, compaction.level)
        lines.append(markdown_text(f"Current level: {level}", limit=200))
        lines.append("")
        lines.append(
            f"_{markdown_text('Compaction measures the messages and the system prompt, not the tool definitions.', limit=200)}_"
        )

    lines += ["", "**Usage by category**", "", _row("Category", "Tokens", "Window"), _row("---", "---:", "---:")]
    for category in report.categories:
        label = CATEGORY_LABELS.get(category.key, category.key)
        lines.append(_row(markdown_text(label), _tokens(category.tokens), _share(category.tokens, report.window)))

    if report.detail:
        for category in report.categories:
            if not category.items:
                continue
            label = CATEGORY_LABELS.get(category.key, category.key)
            lines += ["", f"**{markdown_text(label)}**", "", _row("Item", "Tokens"), _row("---", "---:")]
            for item in category.items:
                name = markdown_text(_item_name(category, item.name))
                if item.group:
                    name = f"{name} \\({markdown_text(item.group, limit=40)}\\)"
                lines.append(_row(name, _tokens(item.tokens)))
    else:
        lines += ["", f"_{markdown_text('Run /context detail for one row per item.')}_"]
    return "\n".join(lines)


def render_help(commands: tuple[ChatCommand, ...]) -> str:
    lines = ["## Commands", "", _row("Command", "What it does"), _row("---", "---")]
    for command in commands:
        lines.append(_row(markdown_text(usage(command)), markdown_text(command.description, limit=200)))
        for argument in command.arguments:
            lines.append(
                _row(
                    markdown_text(f"/{command.name} {argument.name}"),
                    markdown_text(argument.description, limit=200),
                )
            )
    return "\n".join(lines)
