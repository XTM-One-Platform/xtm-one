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

"""Mid-turn segment output finalization.

A ``segment_complete`` event hands a live-streamed mid-turn report over to
the web chat, which persists the text verbatim.  The turn's FINAL answer
goes through a deterministic output pipeline inside the chat services
(``inject_code_images`` then ``guard_urls``) *after* the agent loop returns —
a delivered segment never reaches that code, so without this helper a wait
report could be stored with unguarded platform links or without the charts
it just generated.  Both chat services wrap their ``on_event`` callback with
this helper (only when ``deliver_segments`` is on) so the segment content is
finalized BEFORE it leaves the service layer:

- **Chart injection**: images accumulated so far belong to the report the
  user just read.  They are drained from the shared ``code_images`` list in
  place, so the final answer's own injection pass appends only charts
  generated *after* this segment — never duplicates. The ``render_chart``
  blocks (``charts``) are placed and drained the same way.
- **URL guard**: hallucinated platform deep links are corrected, and
  links on a platform's internal URL shown on its registered URL,
  exactly like the final answer's (``url_guard.finalize_platform_links``),
  behind the same cheap substring gates and the global kill switch.  The
  guard's base URLs are
  expensive to produce (integration scan + per-row ``extra_config``
  decryption, or a DB query on the General Assistant path), so they are
  resolved LAZILY through a caller-supplied resolver that only runs once a
  segment actually passes both gates — mirroring how the final-answer path
  only does this work behind the same gates.  ``make_lazy_base_url_resolver``
  adds the once-per-turn cache: an unresolved cache stays ``None`` (never an
  empty-list sentinel), so a turn whose first segments were unguardable
  still resolves for a later guardable one, while two guardable segments
  share one resolution.

The agent's structured-output contract (JSON repair / format finalization)
is deliberately NOT applied here: a mid-turn segment is progress narration
emitted while the turn continues, the output contract governs the turn's
final deliverable, and "repairing" a narrative checkpoint into JSON would
mangle text the user already read on screen.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.url_guard import PlatformLinkBases

logger = logging.getLogger(__name__)


def make_lazy_base_url_resolver(
    resolve: Callable[[], Awaitable[PlatformLinkBases | list[str]]],
) -> Callable[[], Awaitable[PlatformLinkBases | list[str]]]:
    """Wrap a base-URL resolution coroutine with a once-per-turn cache.

    The cache is ``None`` until the underlying resolver has actually run —
    deliberately distinct from ``[]`` (resolved, no guardable platforms) so
    a turn whose early segments never passed the guard gates still resolves
    for a later guardable segment, while repeat guardable segments reuse the
    first resolution instead of re-scanning / re-querying.
    """
    cache: PlatformLinkBases | list[str] | None = None

    async def _resolver() -> PlatformLinkBases | list[str]:
        nonlocal cache
        if cache is None:
            cache = await resolve()
        return cache

    return _resolver


async def resolve_binding_base_urls(
    integration_bindings: dict | None,
) -> PlatformLinkBases | list[str]:
    """Resolve guard base URLs (and the platforms reached on an internal URL)
    from a FINAL ``{provider: [ids] | id}`` dict.

    Both chat services hand this the bindings dict that survived the whole
    scope-resolution pipeline (direct bindings, template resolution, and the
    web chat's ``session_integration_ids`` override that *replaces* them) —
    never a row cache captured before those swaps, which would leave a
    template- or session-selected platform's ``/dashboard`` links unguarded.

    Runs on a fresh short-lived session because it is invoked from inside
    the agent loop, after the turn's request session released its pooled
    connection — re-using that session here would pin a connection for the
    rest of the loop.
    """
    if not integration_bindings:
        return []
    from app.core.database import async_session
    from app.services.url_guard import collect_platform_link_bases

    async with async_session() as session:
        return await collect_platform_link_bases(session, integration_bindings)


async def finalize_segment_content(
    text: str,
    *,
    code_images: list[str],
    resolve_base_urls: Callable[[], Awaitable[PlatformLinkBases | list[str]]],
    db: object | None,
    user_id: str | None,
    conversation_id: str | None,
    assignment_run_id: str | None = None,
    charts: list | None = None,
) -> str:
    """Apply the final-answer output pipeline to one mid-turn segment.

    Mirrors the post-loop finalization in ``run_agent_chat`` /
    ``run_general_chat``: chart injection first (so injected image markdown
    is also visible to the URL guard), then the platform URL guard.

    ``code_images`` is mutated in place (drained) — the caller shares the
    same list object with its post-loop injection pass, which is what
    prevents a chart from being attached to both the segment and the final
    answer.  Chart persistence never touches ``db``'s transaction:
    ``_persist_generated_file`` always commits on its own short-lived
    session, which is what makes this safe to run from inside the
    agent loop after the request session released its pooled connection.
    """
    if code_images:
        pending = list(code_images)
        from app.services.code_interpreter import inject_code_images

        text = await inject_code_images(
            text,
            pending,
            db=db,
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )
        # Drain only AFTER a successful injection, and only the snapshot
        # that was injected: if ``inject_code_images`` raises, the caller
        # delivers the raw segment and the still-undrained charts remain
        # available for the post-loop final-answer injection instead of
        # vanishing from both messages.
        del code_images[: len(pending)]
    if charts is not None and (charts or "[[CHART:" in (text or "").upper()):
        # The ``render_chart`` blocks of the turn so far, drained in
        # place like ``code_images``: the final answer places only later ones.
        from app.services.chat_charts import inject_charts

        text = inject_charts(text, charts)
    if text:
        # ``finalize_platform_links`` runs the (integration-scan / DB-query +
        # decryption) base URL resolution behind the same gates as the
        # final-answer path.  Best-effort: a resolution or guard failure past
        # this point must not raise, or the charts already injected AND
        # drained above would be lost from both the segment (caller falls
        # back to the raw event) and the final answer (list already drained).
        try:
            from app.services.url_guard import finalize_platform_links

            text = await finalize_platform_links(
                text,
                resolve_base_urls,
                conversation_id=conversation_id,
            )
        except Exception:
            logger.exception(
                "Segment URL guard failed for conversation %s; delivering the unguarded segment text",
                conversation_id,
            )
    return text
