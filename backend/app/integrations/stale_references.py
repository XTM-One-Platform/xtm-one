# SPDX-License-Identifier: Apache-2.0

"""Sustained stale integration references, reported by a provider to the run it serves.

An integration provider knows two things the run executing it does not: that a
call failed because the resource it names does not exist (a 404 on the
resource's own path), and whether that miss is SUSTAINED - the same resource,
through the same integration, missing in separate runs. That second fact is
what tells a configured reference that went stale (a Notion database deleted,
or no longer shared with the integration) from the agent's own typo. The
provider cannot act on it: whether a run fails, and whether an automation
should stop, belongs to whoever runs it.

This module is the one channel between the two. The runner binds a collector
for the run (:func:`collect_stale_references` - a ``ContextVar``: tasks copy
the context at creation, so every tool call of the run, a parallel batch
included, reports into the same collector), the provider calls
:func:`report_stale_reference` once its own detector says the reference is
sustained, and :func:`confirm_reference_alive` when the same resource answers
later in the run.

Contract:

* Reporting never raises and is a no-op outside a collecting run: a chat turn
  binds nothing, so its tools behave exactly as before.
* One entry per provider, integration and resource: probing the same missing
  id as a database, then as a page, is one stale reference.
* A resource confirmed alive in the run is dropped, and a later report of it
  in the same run is ignored - it answered, so it is not stale.
* A collector bound for a set of integrations ignores the others: a consulted
  agent's own integrations are not this run's configuration.

Dependency-free by design: the providers AND the assignment engine import it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StaleReference:
    """One resource an integration keeps reporting missing."""

    #: Provider key, e.g. ``notion``.
    provider: str
    #: Provider display name, e.g. ``Notion``.
    provider_label: str
    integration_id: str
    #: What the resource is, in the provider's words: ``database``, ``page``.
    resource_type: str
    #: The id as the agent wrote it, so a person can search for it.
    resource_id: str
    #: Separate runs the resource was missing in, within the provider's window.
    failing_runs: int


def _normalized(value: str) -> str:
    return str(value).strip().lower()


class StaleReferenceCollector:
    """What one run learned about stale references. Bound by the runner."""

    __slots__ = ("_alive", "_allowed", "_references")

    def __init__(self, integration_ids: Iterable[str] | None = None) -> None:
        self._allowed: frozenset[str] | None = (
            None
            if integration_ids is None
            else frozenset(_normalized(i) for i in integration_ids if i)
        )
        self._references: dict[tuple[str, str, str], StaleReference] = {}
        self._alive: set[tuple[str, str, str]] = set()

    def _key(self, provider: str, integration_id: str, key: str) -> tuple[str, str, str]:
        return (_normalized(provider), _normalized(integration_id), _normalized(key))

    def add(self, reference: StaleReference, *, key: str | None = None) -> None:
        if self._allowed is not None and _normalized(reference.integration_id) not in self._allowed:
            return
        entry = self._key(
            reference.provider, reference.integration_id, key or reference.resource_id
        )
        if entry in self._alive:
            return
        known = self._references.get(entry)
        if known is None or reference.failing_runs > known.failing_runs:
            self._references[entry] = reference

    def confirm_alive(self, provider: str, integration_id: str, key: str) -> None:
        entry = self._key(provider, integration_id, key)
        self._alive.add(entry)
        self._references.pop(entry, None)

    @property
    def references(self) -> tuple[StaleReference, ...]:
        return tuple(self._references.values())


_collector: ContextVar[StaleReferenceCollector | None] = ContextVar(
    "stale_reference_collector", default=None
)


@contextmanager
def collect_stale_references(
    integration_ids: Iterable[str] | None = None,
) -> Iterator[StaleReferenceCollector]:
    """Collect the stale references reported in this context until the block exits.

    *integration_ids* restricts the collector to the integrations the run was
    configured with; ``None`` accepts every integration.
    """
    collector = StaleReferenceCollector(integration_ids)
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def report_stale_reference(
    *,
    provider: str,
    provider_label: str,
    integration_id: str,
    resource_type: str,
    resource_id: str,
    failing_runs: int,
    key: str | None = None,
) -> None:
    """Tell the running collector, if any, that a sustained stale reference was hit.

    *key* is the provider's normalised form of the id (Notion strips dashes),
    so two spellings of one resource are one entry; it defaults to
    *resource_id*.
    """
    collector = _collector.get()
    if collector is None:
        return
    try:
        collector.add(
            StaleReference(
                provider=provider,
                provider_label=provider_label,
                integration_id=str(integration_id),
                resource_type=resource_type,
                resource_id=resource_id,
                failing_runs=int(failing_runs),
            ),
            key=key,
        )
    except Exception:
        logger.debug("Could not record a stale %s reference", provider, exc_info=True)


def confirm_reference_alive(*, provider: str, integration_id: str, key: str) -> None:
    """Tell the running collector, if any, that this resource answered."""
    collector = _collector.get()
    if collector is None:
        return
    try:
        collector.confirm_alive(provider, str(integration_id), key)
    except Exception:
        logger.debug("Could not record a live %s reference", provider, exc_info=True)


def describe_reference(reference: StaleReference, integration_names: Mapping[str, str]) -> str:
    """``the Notion database <id> through the integration "<name>"``."""
    name = integration_names.get(reference.integration_id) or reference.integration_id
    return (
        f"the {reference.provider_label} {reference.resource_type} "
        f'{reference.resource_id} through the integration "{name}"'
    )


def _runs_phrase(reference: StaleReference) -> str:
    return f"not found in {reference.failing_runs} separate runs"


class StaleIntegrationReferenceError(Exception):
    """An otherwise finished run hit a sustained stale integration reference.

    Raised by the assignment engine once the agent loop returned; the message
    is what the run records as its error, and it names each resource, its
    integration and the remedy.
    """

    def __init__(
        self,
        references: Sequence[StaleReference],
        *,
        integration_names: Mapping[str, str] | None = None,
    ) -> None:
        self.references: tuple[StaleReference, ...] = tuple(references)
        self.integration_names: dict[str, str] = dict(integration_names or {})
        super().__init__(self._message())

    def _message(self) -> str:
        if len(self.references) == 1:
            ref = self.references[0]
            return (
                f"Stale integration reference: "
                f"{describe_reference(ref, self.integration_names)} cannot be "
                f"found ({_runs_phrase(ref)}). Share it with that integration in "
                f"{ref.provider_label}, or update the assignment so it no longer "
                "uses it."
            )
        lines = [
            "Stale integration references - these resources cannot be found "
            "through their integration:"
        ]
        lines += [
            f"- {describe_reference(ref, self.integration_names)} ({_runs_phrase(ref)})"
            for ref in self.references
        ]
        lines.append(
            "Share each one with its integration, or update the assignment so it "
            "no longer uses them."
        )
        return "\n".join(lines)
