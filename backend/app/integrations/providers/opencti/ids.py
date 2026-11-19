"""Refuse a short id prefix before any OpenCTI request.

OpenCTI resolves an object by its full internal id (UUID) or a STIX id; an
8-hex prefix such as ``12aa2870`` is passed through as is and answered
``Cant find element to update`` or ``Playbook does not exist for this
entity``, which reads as "the object no longer exists". The XTM One tools
for work items, frictions and traces take such prefixes, so an agent passes
them here too. The guard below refuses them by name, with no request sent.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable
from typing import Any

from app.tools.base import CallableToolExecutor, ToolHandler

# A bare hex string shorter than a UUID written without dashes (32): a prefix
# of an id, never an id OpenCTI resolves. A UUID carries dashes, a STIX id a
# ``type--`` part, so neither matches.
_PREFIX_RE = re.compile(r"[0-9a-fA-F]{8,31}")


def _is_id_argument(name: str) -> bool:
    return name in ("id", "ids") or name.endswith(("_id", "_ids"))


def _prefix_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, str) and _PREFIX_RE.fullmatch(v.strip())]


def short_id_refusal(arguments: dict[str, Any]) -> str | None:
    """The refusal naming every id argument that holds a short prefix, else None."""
    found = [
        f"{name}={', '.join(repr(v) for v in prefixes)}"
        for name, value in sorted(arguments.items())
        if _is_id_argument(name) and (prefixes := _prefix_values(value))
    ]
    if not found:
        return None
    return (
        f"Error: {'; '.join(found)} looks like a short id prefix; OpenCTI resolves "
        "only a full id (its UUID or STIX id) and would answer as if the object did "
        "not exist. Nothing was sent. Pass the full id, read from the list or get "
        "action of the tool."
    )


def _guarded(handler: ToolHandler) -> ToolHandler:
    @functools.wraps(handler)
    async def guarded(**kwargs: Any) -> Any:
        refusal = short_id_refusal(kwargs)
        if refusal is not None:
            return refusal
        return await handler(**kwargs)

    return guarded


def install_short_id_guard(executor: CallableToolExecutor, tool_names: Iterable[str]) -> None:
    """Wrap the handler of every OpenCTI tool in *tool_names* with the guard."""
    for name in tool_names:
        executor.wrap_handler(name, _guarded)
