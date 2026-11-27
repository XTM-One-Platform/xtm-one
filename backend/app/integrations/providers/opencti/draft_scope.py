"""The OpenCTI draft a run works in, kept on the run's scope (#2688).

A tool build sends its requests with a ``headers`` dict of its own, and the
draft the run activates (``use_opencti_draft``), creates (``create_opencti_draft``,
the autonomous gate) or drops (a validated or locked draft) is written into it.
That dict is the build's alone: a run it delegates to (consult, background task,
transfer) builds its own tools from the run's ``scope_headers`` and so started
outside the draft, reading "not found" for every entity that exists only there.

:class:`DraftScopedHeaders` mirrors every change of the draft header into the
run's scope dict, so a delegation forwarding that scope carries the draft the
run is in at that moment. Only the draft header is mirrored: the bearer token
and the other request headers never reach the scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.integrations.http import RefreshableHeaders

#: The request header OpenCTI reads the draft context from.
DRAFT_HEADER = "opencti-draft-id"


class DraftScopedHeaders(RefreshableHeaders):
    """Request headers whose draft header is mirrored into the run's scope.

    *run_scope* is the run's ``scope_headers`` dict (``None``: nothing is
    mirrored, the build behaves as a plain :class:`RefreshableHeaders`).
    """

    def __init__(
        self,
        *args: Any,
        run_scope: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.run_scope = run_scope

    def _mirror(self) -> None:
        if self.run_scope is None:
            return
        draft = super().get(DRAFT_HEADER)
        if draft:
            self.run_scope[DRAFT_HEADER] = draft
        else:
            self.run_scope.pop(DRAFT_HEADER, None)

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        if key == DRAFT_HEADER:
            self._mirror()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        if key == DRAFT_HEADER:
            self._mirror()

    def pop(self, key: str, *default: Any) -> Any:
        value = super().pop(key, *default)
        if key == DRAFT_HEADER:
            self._mirror()
        return value

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        changes = dict(*args, **kwargs)
        super().update(changes)
        if DRAFT_HEADER in changes:
            self._mirror()

    def setdefault(self, key: str, default: Any = None) -> Any:
        value = super().setdefault(key, default)
        if key == DRAFT_HEADER:
            self._mirror()
        return value

    def clear(self) -> None:
        had_draft = DRAFT_HEADER in self
        super().clear()
        if had_draft:
            self._mirror()


def not_found_message(subject: str, headers: Mapping[str, str]) -> str:
    """The answer of a read that found nothing, naming where it looked.

    A bare "not found" let a run tell no real miss from an object that exists
    only in an OpenCTI draft it is not in, and carry on as if it had read it.
    """
    draft = headers.get(DRAFT_HEADER)
    if draft:
        return f"{subject} not found in the live data nor in the active OpenCTI draft {draft}."
    return (
        f"{subject} not found in the live data. It may exist in an OpenCTI draft that is "
        "not active in this run: read it again from that draft, or report the object as "
        "unreadable. Never answer as if it had been read."
    )
