# SPDX-License-Identifier: Apache-2.0

"""The rule for a value put into a Microsoft Graph / Bot Framework URL path segment.

Shared by the Teams bot tools (``teams_bot.py``), the Microsoft Teams tools
(``microsoft/_teams.py``) and the Teams Graph notification verdict
(``channels/teams_graph_verdict.py``), which a worker process imports without
the provider stack: the standard library only.
"""

from __future__ import annotations

import re

# Characters that must never appear in a value interpolated into a URL path
# segment (emails, chat/conversation ids).  Tool inputs are LLM-controlled,
# so a crafted value containing ``/``, ``?`` or ``#`` could redirect the
# request to a different Graph / Bot Framework endpoint under the app token.
# Legitimate emails and Teams conversation ids (``19:...@thread.v2``) never
# contain these characters, so rejecting is safe (and keeps real URLs
# byte-identical, unlike percent-encoding).  ``%`` is refused too, so a value
# decoded once (``%252F`` read as ``%2F``) cannot reach Graph as a ``/``.
_UNSAFE_PATH_SEGMENT = re.compile(r"[/\\?#%\s]")

#: Dot segments: a URL normalizer resolves them, climbing out of the path.
_DOT_SEGMENTS = frozenset({".", ".."})


def _validate_path_segment(value: str, label: str) -> str | None:
    """Return an error string when *value* is unsafe to embed in a URL path."""
    if not value or value in _DOT_SEGMENTS or _UNSAFE_PATH_SEGMENT.search(value):
        return f"Error: Invalid {label}: {value!r}."
    return None
