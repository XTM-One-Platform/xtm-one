# SPDX-License-Identifier: Apache-2.0

"""How a provider trims an event before the run is shown it.

A webhook body carries what its sender chose to put in it, sized for machines:
a GitHub ``push`` names every commit with its whole message, repeats the last
one as ``head_commit`` and lists dozens of ``*_url`` API templates. The run
reads that body as its Trigger Payload, and a large one is cut at the
serialiser's cap (``assignment_helpers._serialize_payload``), losing the end
of the data it needed to keep the copies it did not.

A provider registers a shaper for the events it knows: a pure function
``(event_key, payload) -> payload`` that returns a smaller copy holding what an
agent acts on. The generic layer (``assignment_helpers._build_trigger_information``)
applies it to the payload it shows the run, and only there: the stored run
payload, the ``{{trigger.*}}`` template context and the trigger filter keep the
delivery as it came. A provider without a shaper, or an event its shaper does
not know (it returns the payload unchanged), is shown whole.

Light on purpose (no model, no service import): provider packages register
their shaper when they are imported, like ``event_enrichment``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: ``(event_key, payload) -> payload``: a NEW dict, the argument left untouched.
RunPayloadShaper = Callable[[str, dict], dict]

_SHAPERS: dict[str, RunPayloadShaper] = {}


def register_run_payload_shaper(provider: str, shaper: RunPayloadShaper) -> None:
    """Register *provider*'s shaper. Idempotent (last wins)."""
    _SHAPERS[provider] = shaper


def run_payload_shaper(provider: str) -> RunPayloadShaper | None:
    """The shaper *provider* registered, ``None`` when it has none."""
    return _SHAPERS.get(provider)


def shape_run_payload(provider: object, event_key: object, payload: dict) -> dict:
    """*payload* as the run is shown it: the provider's shaper applied, else unchanged.

    A shaper that raises shows the delivery whole (logged): a trim is an
    economy, never a reason to hide the event from the run.
    """
    if not isinstance(provider, str) or not isinstance(event_key, str):
        return payload
    shaper = _SHAPERS.get(provider)
    if shaper is None:
        return payload
    try:
        shaped = shaper(event_key, payload)
    except Exception:
        logger.warning("Run payload shaper of %s failed on %s; payload shown whole", provider, event_key, exc_info=True)
        return payload
    return shaped if isinstance(shaped, dict) else payload
