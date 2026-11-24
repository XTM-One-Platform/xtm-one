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

"""Artifact markers as links in the text a send tool hands an external surface.

The web chat draws ``[[PRESENTATION:id]]``, ``[[REPORT:id]]``, ``[[VIDEO:id]]``,
``[[DASHBOARD:id]]`` and ``[[FILE:id]]`` as cards. A message an agent sends
through an integration (a Teams chat or channel, an Outlook or Gmail mail, a
Slack post, a channel bot) is read on a surface that draws none of them: the
reader got the raw marker. The channel reply path (``channels/router.py``)
already resolves them; this module applies the same resolvers, with the same
scope, to the text arguments of every send tool, before the provider runs.

A tool names those arguments with :attr:`ToolDefinition.outbound_text` (by
default the body arguments of its :attr:`ToolDefinition.send_guard`).

Scope: a marker is LLM text, so it is only linked when it names a row of the
run's conversation or user (the channel resolvers' rule); an unknown or
out-of-scope id is dropped, never left raw and never linked. With neither a
conversation nor a user every marker is dropped (fail closed).
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Mapping
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: Any artifact marker the web chat draws as a card.
_ANY_MARKER_RE = re.compile(
    r"\[\[(?:FILE|PRESENTATION|REPORT|VIDEO|DASHBOARD|INVESTIGATION|WORK_ITEM):",
    re.IGNORECASE,
)
#: An artifact marker other than a file's.
_NON_FILE_MARKER_RE = re.compile(
    r"\[\[(?:PRESENTATION|REPORT|VIDEO|DASHBOARD|INVESTIGATION|WORK_ITEM):",
    re.IGNORECASE,
)


def outbound_text_arguments(definition: ToolDefinition) -> tuple[str, ...]:
    """The arguments of *definition* whose text reaches a surface with no card renderer."""
    if definition.outbound_text:
        return definition.outbound_text
    if definition.send_guard is not None:
        # A subject is a header line: a Markdown link there reads as noise.
        return tuple(name for name in definition.send_guard.body if name != "subject")
    return ()


def has_artifact_marker(text: object, *, keep_file_markers: bool = False) -> bool:
    """Whether *text* is a string carrying at least one artifact marker to resolve.

    With *keep_file_markers*, a ``[[FILE:id]]`` marker does not count.
    """
    pattern = _NON_FILE_MARKER_RE if keep_file_markers else _ANY_MARKER_RE
    return isinstance(text, str) and pattern.search(text) is not None


async def markers_as_outbound_links(
    db: Any,
    text: str,
    *,
    conversation_id: object = None,
    user_id: object = None,
    language: str | None = None,
    keep_file_markers: bool = False,
) -> str:
    """*text* with every artifact marker as a link, or dropped when it cannot be one.

    Files get their signed public download link on the API URL; decks,
    reports, videos and dashboards their page on the web app; an investigation
    and a scheduled work item, which have no page of their own, are dropped.
    With *keep_file_markers* the ``[[FILE:id]]`` markers are left as they are,
    for a send tool that delivers them as files
    (:attr:`ToolDefinition.outbound_keeps_file_markers`).
    """
    if not has_artifact_marker(text, keep_file_markers=keep_file_markers):
        return text
    from app.channels.base import (
        resolve_channel_file_markers,
        strip_channel_investigation_markers,
        strip_channel_work_item_markers,
    )
    from app.core.config import settings
    from app.services.platform_chat_artifacts import markers_as_links

    scope = {"conversation_id": conversation_id, "user_id": user_id, "language": language}
    if not keep_file_markers:
        text = await resolve_channel_file_markers(
            db, text, settings.base_url, conversation_id=conversation_id, user_id=user_id, language=language
        )
    text = await markers_as_links(db, text, **scope)
    text = strip_channel_investigation_markers(text)
    return strip_channel_work_item_markers(text)


async def _outbound_arguments(
    kwargs: Mapping[str, Any],
    names: tuple[str, ...],
    context: Mapping[str, Any],
    keep_file_markers: bool = False,
) -> dict[str, Any]:
    """*kwargs* with the string arguments in *names* made clickable.

    A bare reference to the platform's host gets its scheme
    (:func:`~app.services.outbound_links.bare_platform_links_as_urls`, pure, no
    session); an artifact marker is resolved in the run's scope.
    """
    from app.services.outbound_links import bare_platform_links_as_urls

    resolved = dict(kwargs)
    for name in names:
        if isinstance(resolved.get(name), str):
            resolved[name] = bare_platform_links_as_urls(resolved[name])
    targets = [name for name in names if has_artifact_marker(resolved.get(name), keep_file_markers=keep_file_markers)]
    if not targets:
        return resolved
    from app.core.database import async_session
    from app.core.utils import safe_uuid

    linked = dict(resolved)
    scope: dict[str, Any] = {
        "conversation_id": safe_uuid(context.get("conversation_id")),
        "user_id": safe_uuid(context.get("user_id")),
    }
    if keep_file_markers:
        scope["keep_file_markers"] = True
    try:
        # Its own session: integration tools run concurrently in a turn, and
        # an AsyncSession is single-connection (see _make_persist_file_fn).
        async with async_session() as db:
            for name in targets:
                linked[name] = await markers_as_outbound_links(db, resolved[name], **scope)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to resolve artifact markers of a send: %s", format_exception_message(exc))
        return resolved
    return linked


def send_markers_as_links(
    defs: list[ToolDefinition],
    executor: CallableToolExecutor,
    context: Mapping[str, Any],
) -> None:
    """Re-register every send tool of *defs* so its outbound text carries links, not markers or bare hosts.

    The handler then receives its :func:`outbound_text_arguments` with each
    artifact marker resolved in the run's scope (*context*: ``user_id``,
    ``conversation_id``); every other argument and the handler itself are
    unchanged. ``functools.wraps`` keeps the signature the executor filters on.
    A tool with :attr:`ToolDefinition.outbound_keeps_file_markers` keeps its
    ``[[FILE:id]]`` markers: it delivers them as files itself.
    """

    def _linked(handler: Any, names: tuple[str, ...], keep_file_markers: bool) -> Any:
        @functools.wraps(handler)
        async def _linked_handler(*args: Any, **kwargs: Any) -> Any:
            arguments = await _outbound_arguments(kwargs, names, context, keep_file_markers)
            return await handler(*args, **arguments)

        return _linked_handler

    for d in defs:
        names = outbound_text_arguments(d)
        if names:
            executor.wrap_handler(
                d.name,
                functools.partial(_linked, names=names, keep_file_markers=d.outbound_keeps_file_markers),
            )
