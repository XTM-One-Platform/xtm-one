# SPDX-License-Identifier: Apache-2.0

"""Renamed business verticals and intent namespaces.

A platform names its business vertical and its intents at registration
(``POST /platform/register``) and reads them back through
``GET /intents/catalog``. When a vertical is renamed, a platform build that
predates the rename keeps sending the old name until it is upgraded. Left
alone, its next heartbeat would recreate the old vertical and the old intents
next to the migrated ones, and its catalog read would find no binding.

Every name a platform sends goes through :func:`canonical_vertical` and
:func:`canonical_intent_name` at those two boundaries, so the stored rows and
the bindings an administrator manages keep a single spelling. The stored rows
themselves are rewritten by the Alembic revision that performs the rename.
"""

from __future__ import annotations

#: Former vertical name -> current one. The vertical is also the namespace of
#: the intents specific to it (``<vertical>.<capability>``), so an intent in a
#: former namespace moves with it.
RENAMED_VERTICALS: dict[str, str] = {
    # Cyber risk quantification, formerly governance, risk and compliance.
    "grc": "crq",
}


def canonical_vertical(name: str | None) -> str | None:
    """The current name of a possibly renamed business vertical."""
    if not name:
        return name
    return RENAMED_VERTICALS.get(name, name)


def canonical_intent_name(name: str) -> str:
    """The current name of an intent whose namespace may have been renamed."""
    namespace, dot, capability = name.partition(".")
    if not dot:
        return name
    current = RENAMED_VERTICALS.get(namespace)
    return f"{current}.{capability}" if current else name
