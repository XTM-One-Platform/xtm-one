# SPDX-License-Identifier: Apache-2.0

"""Directory lookups: the person behind an email, read from an HR directory.

A provider that is a company directory registers here a lookup that takes the
integration's compound token and an email and answers a
:class:`DirectoryProfile` (or ``None`` when nobody in the directory has that
email). The lookup raises on any failure; the caller decides what a failure
means for its turn (``services/sender_directory.py``).

A profile carries the allow-listed fields below and nothing else: a lookup
requests only the directory fields these are read from, so a sensitive field
(pay, performance, personal contact, health, equity) never reaches the
platform, whatever the credential is allowed to read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

#: Most direct reports named in a profile; past it only the count is given.
MAX_NAMED_REPORTS = 10


@dataclass(frozen=True)
class DirectoryProfile:
    """The allow-listed directory fields of one person."""

    display_name: str = ""
    title: str = ""
    department: str = ""
    sub_department: str = ""
    site: str = ""
    timezone: str = ""
    manager_name: str = ""
    manager_title: str = ""
    direct_report_count: int = 0
    direct_report_names: tuple[str, ...] = field(default_factory=tuple)
    start_date: str = ""


DirectoryLookup = Callable[[str, str], Awaitable[DirectoryProfile | None]]


def directory_lookup(provider: str) -> DirectoryLookup | None:
    """The directory lookup *provider* registers, or ``None``."""
    return _lookups().get(provider)


def directory_providers() -> frozenset[str]:
    """The providers that are a company directory."""
    return frozenset(_lookups())


def _lookups() -> dict[str, DirectoryLookup]:
    from app.integrations.providers.hibob import lookup_directory_profile as hibob_lookup

    return {"hibob": hibob_lookup}
