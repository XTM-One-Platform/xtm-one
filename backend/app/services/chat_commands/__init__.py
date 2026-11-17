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

"""Chat slash commands (#6590): one registry for every chat surface.

A command is answered by the platform, never by the model: it is not sent to
the agent, consumes no agentic quota, is never stored in the conversation and
does not move the context gauge. Every surface parses with
:func:`parse_command`, so ``/context detail`` means the same thing in the web
composer, the chat embedded in a module and a channel; anything it does not
recognise exactly (``/etc/hosts``, ``/contextual``, ``/context foo``) is an
ordinary message.

Design: ``dev-docs/chat-slash-commands.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The web chat. The embedded chat and the channels join as they are wired.
SURFACE_WEB = "web"
SURFACES: tuple[str, ...] = (SURFACE_WEB,)


@dataclass(frozen=True)
class CommandArgument:
    """An argument a command accepts, under its name or one of its aliases."""

    name: str
    aliases: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class ChatCommand:
    name: str
    description: str
    arguments: tuple[CommandArgument, ...] = ()
    surfaces: frozenset[str] = frozenset(SURFACES)

    def argument(self, token: str) -> CommandArgument | None:
        """The argument *token* names, case-insensitively, or ``None``."""
        lowered = token.lower()
        for argument in self.arguments:
            if lowered == argument.name or lowered in argument.aliases:
                return argument
        return None


COMMAND_CONTEXT = "context"
COMMAND_HELP = "help"
ARGUMENT_DETAIL = "detail"

COMMANDS: tuple[ChatCommand, ...] = (
    ChatCommand(
        name=COMMAND_CONTEXT,
        description="Show how this conversation uses its context window.",
        arguments=(
            CommandArgument(
                name=ARGUMENT_DETAIL,
                aliases=("-d",),
                description="Add one row per tool, skill, memory and knowledge source.",
            ),
        ),
    ),
    ChatCommand(name=COMMAND_HELP, description="List the commands available here."),
)

_COMMANDS_BY_NAME: dict[str, ChatCommand] = {command.name: command for command in COMMANDS}

#: Longest text worth parsing: a command and one argument. Anything longer is
#: a message, and is never split.
_MAX_COMMAND_CHARS = 64


@dataclass(frozen=True)
class ParsedCommand:
    command: ChatCommand
    argument: CommandArgument | None = None


def commands_for(surface: str) -> tuple[ChatCommand, ...]:
    """The commands *surface* offers, in registry order."""
    return tuple(command for command in COMMANDS if surface in command.surfaces)


def parse_command(text: str, surface: str) -> ParsedCommand | None:
    """The command *text* is on *surface*, or ``None`` for an ordinary message.

    The trimmed text must be ``/<name>`` or ``/<name> <argument>`` exactly,
    case-insensitively, naming a command *surface* offers and one of its
    arguments.
    """
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) > _MAX_COMMAND_CHARS:
        return None
    # The name follows the slash: "/ context" is a sentence.
    if len(stripped) < 2 or stripped[1].isspace():
        return None
    tokens = stripped[1:].split()
    if not tokens or len(tokens) > 2:
        return None
    command = _COMMANDS_BY_NAME.get(tokens[0].lower())
    if command is None or surface not in command.surfaces:
        return None
    if len(tokens) == 1:
        return ParsedCommand(command)
    argument = command.argument(tokens[1])
    return ParsedCommand(command, argument) if argument else None


def usage(command: ChatCommand) -> str:
    """How *command* is typed: ``/context [detail]``."""
    if not command.arguments:
        return f"/{command.name}"
    return f"/{command.name} [{' | '.join(argument.name for argument in command.arguments)}]"
