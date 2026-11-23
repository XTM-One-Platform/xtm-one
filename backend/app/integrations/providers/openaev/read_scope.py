# SPDX-License-Identifier: Apache-2.0

"""The caller's OpenAEV read scope, so an empty search is never read as an empty platform.

OpenAEV filters every list and search by the caller's rights and answers an
EMPTY page, never 403, when the caller lacks them: ``ScenarioService.readable()``
keeps everything only for an admin, a BYPASS holder or ``ACCESS_ASSESSMENT``,
else only what the user holds an OBSERVER grant on. Simulations (exercises) and
atomic testings follow the same rule, findings under ``ACCESS_FINDINGS``.

A count read under a grant-limited identity is therefore what that identity
can see, not the platform total. ``ReadScope`` reads ``GET /api/me`` once per
tool set (``user_admin``, ``user_is_admin_or_bypass``, ``user_capabilities``,
which OpenAEV already expands from BYPASS) and the reads prefix their answer
with :func:`scope_line` when the scope is not full. A failed ``/api/me`` is an
UNKNOWN scope, never a full one.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, Literal

from . import _common

#: The capability that reads every object of an area, by area.
AREA_CAPABILITY: Final = {
    "assessment": "ACCESS_ASSESSMENT",  # scenarios, simulations, atomic testings
    "findings": "ACCESS_FINDINGS",
}

ScopeState = Literal["full", "limited", "unknown"]

LIMITED_LINE: Final = (
    "Scope: this OpenAEV identity reads only what it holds a grant on; counts below "
    "are what it can see, not the platform total."
)
UNKNOWN_LINE: Final = (
    "Scope: unknown (GET /api/me failed: {cause}); counts below may be limited to what "
    "this identity holds a grant on, not the platform total."
)
LIMITED_ZERO: Final = "0 visible to this identity (scope-limited; not proof the platform is empty)"
UNKNOWN_ZERO: Final = "0 visible to this identity (scope unknown; not proof the platform is empty)"


class ReadScope:
    """Lazily-read, per-tool-set snapshot of the caller's read rights (``GET /api/me``)."""

    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self._base_url = base_url
        self._headers = headers
        self._loaded = False
        self._me: dict[str, Any] = {}
        self._error: str | None = None
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                # Looked up at call time, so a stub of ``_common._api`` reaches it.
                resp, err = await _common._api(
                    self._base_url,
                    self._headers,
                    "get",
                    "/api/me",
                    "read the caller's rights",
                    retry_transient=True,
                )
                if err:
                    self._error = err.strip().splitlines()[0][:160] if err.strip() else "no answer"
                else:
                    data = resp.json() if resp is not None else None
                    if isinstance(data, dict):
                        self._me = data
                    else:
                        self._error = "the answer is not a user"
            except Exception as exc:  # a 200 with a non-JSON page: report, never raise
                self._error = f"answer unreadable: {exc}"
            self._loaded = True

    async def state(self, area: str) -> tuple[ScopeState, str]:
        """``(state, cause)`` for *area*: full, limited, or unknown with the failure cause."""
        await self._load()
        if self._error is not None:
            return "unknown", self._error
        me = self._me
        if me.get("user_admin") is True or me.get("user_is_admin_or_bypass") is True:
            return "full", ""
        held = me.get("user_capabilities")
        if not isinstance(held, list):
            return "unknown", "no user_capabilities in the answer"
        names = {str(c) for c in held}
        if "BYPASS" in names or AREA_CAPABILITY[area] in names:
            return "full", ""
        return "limited", ""


def scope_line(state: ScopeState, cause: str = "") -> str:
    """The line an answer starts with under *state*: empty when the scope is full."""
    if state == "limited":
        return LIMITED_LINE
    if state == "unknown":
        return UNKNOWN_LINE.format(cause=cause or "no answer")
    return ""


def count_text(value: int, state: ScopeState) -> str:
    """A count as written in an answer: a zero under a partial scope says what it is."""
    if value == 0 and state == "limited":
        return LIMITED_ZERO
    if value == 0 and state == "unknown":
        return UNKNOWN_ZERO
    return str(value)


async def area_scope(headers: Any, area: str) -> tuple[ScopeState, str]:
    """The tool set's scope for *area*; ``full`` when no resolver is bound (a bare collector)."""
    resolver = getattr(headers, "read_scope", None)
    if not isinstance(resolver, ReadScope):
        return "full", ""
    return await resolver.state(area)


def with_scope(answer: str, state: ScopeState, cause: str = "") -> str:
    """*answer* prefixed with the scope line when the scope is not full."""
    line = scope_line(state, cause)
    return f"{line}\n{answer}" if line else answer


__all__ = [
    "AREA_CAPABILITY",
    "LIMITED_LINE",
    "LIMITED_ZERO",
    "ReadScope",
    "ScopeState",
    "area_scope",
    "count_text",
    "scope_line",
    "with_scope",
]
