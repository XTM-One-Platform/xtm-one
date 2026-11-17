# SPDX-License-Identifier: Apache-2.0

"""Provider connectivity probes (integration seam).

``test_provider_connectivity`` verifies an integration by making one
authenticated HTTP call: the provider is looked up in a table of
``(url, headers)`` pairs, the call is made, and the status code is the verdict.
That contract fits every integration whose health *is* "can we reach their API
with this credential" — which is nearly all of them, and those keep using it.

It fits nothing else. An integration whose health lives in **our own state**
(synced rules, an installed engine, a local dataset) has no endpoint to call
and often no credential at all. Today such a provider either falls through to
the ``"{name} appears configured"`` fallback — a green banner that verified
nothing — or never reaches the check, because the endpoint bails out earlier
with "No credentials stored for this integration".

Rather than growing one ``elif provider == ...`` per such integration, a
provider registers a **probe**: a coroutine returning a :class:`ProbeResult`.
Registration is a module-import side-effect, exactly like
:mod:`app.integrations.lifecycle`, so a provider owns the definition of its own
health and the central test endpoint stays generic.

The contract a probe must honour:

* **It owns the whole verdict.** A registered probe short-circuits credential
  resolution, the ``checks`` table, and the "appears configured" fallback.
  That is what makes credential-less providers testable at all: the probe
  decides whether a missing token is a problem, not the endpoint.
* **Three outcomes, no more.** ``ok`` (works), ``degraded`` (usable but
  limited — surfaced as the API's ``info`` status and its blue banner) and
  ``error`` (does not work). "Configured" is not an outcome: a probe that
  cannot verify anything should not exist.
* **Read-only and cheap.** It runs on a UI button press — no writes, no LLM
  calls, no full re-sync.
* **Never raises, never hangs.** The dispatcher enforces both, so the
  never-raises contract of ``test_provider_connectivity`` survives a buggy
  probe; a probe should still return a useful message for its own failures
  rather than leaning on that safety net.
* **Never echoes a credential** into ``message``: it is rendered in the UI and
  written to the audit trail.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.integration import Integration

logger = logging.getLogger(__name__)

#: Hard ceiling on a single probe. Matches ``CONNECTIVITY_TIMEOUT`` — the test
#: endpoint is synchronous from the user's point of view, so a probe that hangs
#: must fail the same way an unreachable provider does.
PROBE_TIMEOUT = 15  # seconds

#: A probe's verdict.
ProbeStatus = Literal["ok", "degraded", "error"]

#: Verdict → the status the ``/integrations/{id}/test`` response carries.
#: ``degraded`` maps to ``info`` because that is the wire value the frontend
#: already renders as a blue "works, with caveats" banner.
_WIRE_STATUS: dict[str, str] = {"ok": "ok", "degraded": "info", "error": "error"}


@dataclass(frozen=True)
class ProbeContext:
    """Everything a probe is given about the integration under test.

    ``db`` and ``integration`` are always populated when the probe runs from
    ``POST /integrations/{id}/test``. They are typed optional because
    ``test_provider_connectivity`` is also called with credentials alone (from
    provider unit tests); a probe that needs them must say so in its result
    instead of dereferencing ``None``.
    """

    provider: str
    name: str
    token: str | None
    extra: dict
    db: AsyncSession | None = None
    integration: Integration | None = None
    #: Whether the caller may manage the integration (``helpers.can_manage``).
    #: A probe whose message can quote text kept for the people who fix the
    #: integration - a stored sync error - quotes it only when this is true, and
    #: a fixed sentence otherwise. False when the caller is unknown.
    caller_can_manage: bool = False
    #: Who runs the test: a probe that builds the integration's tools acts with
    #: this person's account (on an integration shared without its connected
    #: account, their own).
    acting_user_id: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """A probe's verdict plus the one line the user will read."""

    status: ProbeStatus
    message: str

    @classmethod
    def ok(cls, message: str) -> ProbeResult:
        """Everything the integration promises is operational."""
        return cls("ok", message)

    @classmethod
    def degraded(cls, message: str) -> ProbeResult:
        """Usable, but not at full capability — say which part is missing."""
        return cls("degraded", message)

    @classmethod
    def failed(cls, message: str) -> ProbeResult:
        """Not operational — say what to fix."""
        return cls("error", message)

    @property
    def is_healthy(self) -> bool:
        """True when the integration works, degraded or not."""
        return self.status in ("ok", "degraded")

    def as_response(self) -> dict[str, str]:
        """Render as the ``{"status", "message"}`` body the test endpoint returns."""
        return {"status": _WIRE_STATUS[self.status], "message": self.message}


ConnectivityProbe = Callable[[ProbeContext], Awaitable[ProbeResult]]

_PROBES: dict[str, ConnectivityProbe] = {}


def register_connectivity(provider: str, probe: ConnectivityProbe) -> None:
    """Register *provider*'s connectivity probe. Idempotent (last wins)."""
    _PROBES[provider] = probe


def has_connectivity_probe(provider: str) -> bool:
    """Whether *provider* owns its connectivity verdict through a probe.

    The test endpoint consults this before resolving a credential: a
    probe-backed provider must reach its probe even with nothing stored to
    authenticate with.
    """
    return provider in _PROBES


async def dispatch_connectivity(ctx: ProbeContext) -> ProbeResult | None:
    """Run the provider's probe, or return ``None`` if it has none.

    ``None`` means "not my business" and sends the caller back to the standard
    HTTP checks. Anything else is the final verdict. Never raises: a probe that
    hangs, explodes, or returns the wrong type becomes an ``error`` result.
    """
    probe = _PROBES.get(ctx.provider)
    if probe is None:
        return None
    try:
        result = await asyncio.wait_for(probe(ctx), timeout=PROBE_TIMEOUT)
    except TimeoutError:
        logger.warning("Connectivity probe timed out for provider %s", ctx.provider)
        return ProbeResult.failed(
            f"{ctx.name} health check timed out after {PROBE_TIMEOUT}s."
        )
    except Exception:
        logger.exception("Connectivity probe failed for provider %s", ctx.provider)
        return ProbeResult.failed(f"{ctx.name} health check failed unexpectedly.")
    if not isinstance(result, ProbeResult):
        logger.error(
            "Connectivity probe for provider %s returned %r, not a ProbeResult",
            ctx.provider,
            type(result).__name__,
        )
        return ProbeResult.failed(f"{ctx.name} health check returned no verdict.")
    return result
