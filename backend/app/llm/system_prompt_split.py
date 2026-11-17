# SPDX-License-Identifier: Apache-2.0

"""Split a system prompt into its stable prefix and its volatile tail.

A system prompt opens with what is the same on every run of an agent
configuration (persona, guardrails, attached prompts, skills, policies) and
ends with what changes on every run or turn (the clock, the user's local
time, the fenced memory with its recent activity). A provider that caches an
explicit prompt prefix (Anthropic) puts its breakpoint at the end of the
stable prefix, so the next run reads it from the cache instead of writing it
again; the volatile tail rides after it, covered by the conversation
breakpoint within the run.

The tail starts at the clock line, which
:func:`app.services.builtin_tools.prompting.format_datetime_prompt` writes with
:data:`SYSTEM_VOLATILE_TAIL_MARKER`. The prompt builders place every per-run
section after it.
"""

from __future__ import annotations

#: Opening words of the clock line: where the volatile tail of a system prompt
#: starts. ``format_datetime_prompt`` is the one writer of the line.
SYSTEM_VOLATILE_TAIL_MARKER = "Current date and time (UTC): "


def split_system_prompt(text: str) -> tuple[str, str]:
    """Return ``(stable, volatile)`` with ``stable + volatile == text``.

    The volatile tail starts at the line break(s) before the first clock line.
    A prompt without a clock line, or one whose prefix before it is blank, is
    stable whole: ``(text, "")``.
    """
    idx = text.find(SYSTEM_VOLATILE_TAIL_MARKER)
    if idx <= 0:
        return text, ""
    # The line breaks before the clock line belong to the tail, so the stable
    # block ends on its last character of content.
    while idx > 0 and text[idx - 1] == "\n":
        idx -= 1
    stable = text[:idx]
    if not stable.strip():
        return text, ""
    return stable, text[idx:]
