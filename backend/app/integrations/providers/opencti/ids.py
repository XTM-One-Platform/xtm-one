"""Refuse a short id prefix before any OpenCTI request.

OpenCTI resolves an object by its full internal id (UUID) or a STIX id; an
8-hex prefix such as ``12aa2870`` is passed through as is and answered
``Cant find element to update`` or ``Playbook does not exist for this
entity``, which reads as "the object no longer exists". The guard lives in
``app.integrations.short_id``, shared with the OpenAEV tools; this module
binds it to OpenCTI's name and id forms.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.integrations import short_id
from app.tools.base import CallableToolExecutor

_PLATFORM = "OpenCTI"
_FULL_ID = "a full id (its UUID or STIX id)"


def short_id_refusal(arguments: dict[str, Any]) -> str | None:
    """The refusal naming every id argument that holds a short prefix, else None."""
    return short_id.short_id_refusal(arguments, platform=_PLATFORM, full_id=_FULL_ID)


def install_short_id_guard(executor: CallableToolExecutor, tool_names: Iterable[str]) -> None:
    """Wrap the handler of every OpenCTI tool in *tool_names* with the guard."""
    short_id.install_short_id_guard(executor, tool_names, platform=_PLATFORM, full_id=_FULL_ID)
